"""Real supervisor/control sockets plus independent forwarding echo children."""
from pathlib import Path
import os
import tempfile
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from podgrove import runtime, state
from podgrove.config import Config
from podgrove.forward import Tunnel, free_port
from podgrove.sync import SnapshotRace
from podgrove.session_status import observed
from test_forward_recovery import LocalKube, echo, wait_until
from test_sync import FakeSynchronizer


@pytest.fixture
def session(tmp_path, monkeypatch, request):
    root = tmp_path / "worktree"
    root.mkdir()
    source = root / "source"
    source.write_text("initial")
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    ident = state.identity(root)
    token = uuid.uuid4().hex
    socket_path = Path(tempfile.gettempdir()) / f"podgrove-{os.getuid()}-{token[:16]}.sock"
    path = state.state_path(root, "offline-test")
    data = {"identity": ident, "root": str(root), "context": "offline-test", "namespace": "offline-owned",
            "namespace_mode": "shared", "timeout": 5, "status": "starting", "token": token,
            "socket": str(socket_path), "ttl_seconds": 3600}
    state.write(path, data)
    settings = getattr(request, "param", {})
    kube = LocalKube(tmp_path / "control", settings.get("modes", ("normal",)))
    # The child fixture verifies the target explicitly; use this worktree's
    # generated identity instead of the independent Tunnel fixture identity.
    for resource in (kube.controller, kube.pod):
        resource["metadata"]["name"] = resource["metadata"]["name"].replace("012345abcdef", ident)
        resource["metadata"]["labels"]["podgrove.dev/environment"] = ident
    kube.pod["metadata"]["ownerReferences"][0]["name"] = "pg-" + ident
    original_command = kube.command
    def command(*args):
        substituted = list(args)
        substituted[1] = "pod/pg-012345abcdef-0"
        return original_command(*substituted)
    kube.command = command
    kube.wait = Mock()
    kube.heartbeat = Mock()
    kube.destroy = Mock()
    monkeypatch.setattr(runtime, "Kube", lambda *_args, **_kwargs: kube)
    monkeypatch.setattr(runtime.signal, "signal", lambda *_: None)
    port = free_port()
    config = Config(root=root, files=[], ttl_seconds=3600,
                    forward=[{"service": "api", "port": 8080, "local": port}])
    compose = Mock(config=config)
    compose.model.return_value = {"name": "offline-fixture", "services": {"api": {"image": "offline-fixture"}}}
    compose.published_ports.return_value = [{"service": "api", "target": 8080,
                                            "published": settings.get("published", 8080)}]
    compose.has_watch.return_value = False
    monkeypatch.setattr(runtime, "load_config", lambda *_: config)
    monkeypatch.setattr(runtime, "Compose", lambda *_: compose)
    monkeypatch.setattr(runtime, "run", Mock())
    rows = [{"Service": "api", "Project": "offline-fixture", "State": "running", "Health": "healthy",
             "Publishers": [{"TargetPort": 8080, "PublishedPort": 8080, "Protocol": "tcp", "URL": "0.0.0.0"}]}]
    sync = FakeSynchronizer(root, [source], identity=ident)
    sync.start()
    sync_close = Mock(wraps=sync.close)
    sync.close = sync_close
    monkeypatch.setattr(runtime, "launch_stack", lambda *_: (sync, .01, rows))
    monkeypatch.setattr(runtime, "service_status", lambda *_: rows)
    api = Mock()
    api.start.return_value = api
    api.snapshot.return_value = {"verification": {"state": "verified"}, "failed_connections": 0}
    monkeypatch.setattr(runtime, "DockerTunnel", lambda *_: api)
    tunnels = []
    def app(*args):
        instance = Tunnel(*args, poll_interval=.03, retry_delays=(.2, .03), verification_interval=30)
        tunnels.append(instance)
        return instance
    monkeypatch.setattr(runtime, "Tunnel", app)
    result = []
    thread = threading.Thread(target=lambda: result.append(runtime.serve(path)), daemon=True)
    thread.start()
    wait_until(lambda: state.read(path)["status"] in ("ready", "error"))
    assert state.read(path)["status"] == "ready", state.read(path)
    current = SimpleNamespace(path=path, data=data, source=source, sync=sync, kube=kube, api=api,
                              tunnel=tunnels[0], port=port, thread=thread, result=result, rows=rows)
    try:
        yield current
    finally:
        if thread.is_alive():
            runtime.control(data, "stop")
            thread.join(timeout=5)
        assert not thread.is_alive()
        assert not socket_path.exists()
        assert (tunnels[0].process is None or tunnels[0].process.poll() is not None) and not tunnels[0]._thread.is_alive()
        assert tunnels[0].log is None
        api.close.assert_called_once()
        kube.destroy.assert_not_called()


def test_source_churn_degrades_sync_without_tearing_down_forward_or_control(session, monkeypatch):
    original = session.sync._add_file
    release = threading.Event()
    def racing(*args):
        if not release.is_set():
            raise SnapshotRace("source actively changing")
        return original(*args)
    monkeypatch.setattr(session.sync, "_add_file", racing)
    child = session.tunnel.process
    session.source.write_text("stable when released")
    wait_until(lambda: state.read(session.path)["sync_status"]["state"] == "retrying")
    snapshot = state.read(session.path)
    assert snapshot["status"] == "degraded"
    assert snapshot["ports"][0]["status"] == "ready"
    assert runtime.control(session.data, "ping")["sync_status"]["state"] == "retrying"
    echo(session.port)
    assert session.tunnel.process is child and child.poll() is None
    session.sync.close.assert_not_called()
    release.set()
    wait_until(lambda: state.read(session.path)["sync_status"]["state"] == "ready")
    assert state.read(session.path)["status"] == "ready"
    assert session.sync.transfers[-1]["podgrove-transfer/payload/source"] == b"stable when released"
    echo(session.port)


def test_idle_forward_exit_publishes_reconnecting_then_recovers_same_address(session):
    before = state.read(session.path)["ports"][0]
    first = session.tunnel.process
    first.terminate()
    first.wait(timeout=2)
    wait_until(lambda: state.read(session.path)["forward_status"]["state"] == "reconnecting")
    ping = runtime.control(session.data, "ping")
    assert ping["ok"] and ping["status"] == "degraded"
    assert ping["forward_status"]["state"] == "reconnecting"
    wait_until(lambda: state.read(session.path)["forward_status"]["state"] == "ready")
    after = state.read(session.path)
    assert after["status"] == "ready" and after["ports"][0] == before
    assert session.tunnel.process is not first and session.thread.is_alive()
    echo(session.port)


def test_verification_outage_is_visible_without_disconnecting_control_or_endpoints(session):
    diagnostics = {"verification": {"state": "unavailable", "reason": "metadata_unavailable",
                                    "max_age_seconds": 120, "age_seconds": 31},
                   "active_connections": 1, "failed_connections": 0, "last_failure": None}
    session.api.snapshot.return_value = diagnostics
    ping = runtime.control(session.data, "ping")
    snapshot = observed(state.read(session.path), connected=True, ping=ping)
    assert ping["ok"] and ping["docker_status"] == diagnostics
    assert snapshot["status"] == "degraded"
    assert snapshot["forward_status"]["state"] == "ready"
    echo(session.port)
    session.api.snapshot.return_value = {**diagnostics, "verification": {"state": "verified", "age_seconds": 0}}
    runtime.control(session.data, "touch")
    wait_until(lambda: state.read(session.path)["status"] == "ready")
    ping = runtime.control(session.data, "ping")
    snapshot = observed(state.read(session.path), connected=True, ping=ping)
    assert snapshot["status"] == "ready"
    session.api.close.assert_not_called()


def test_health_diagnostics_larger_than_one_socket_read_do_not_disconnect_session(session, monkeypatch):
    monkeypatch.setattr(runtime, "HEALTH_INTERVAL", 0)
    def unavailable(*_):
        raise runtime.TransientDockerReadError("Temporary status failure: " + "x" * 5000)
    monkeypatch.setattr(runtime, "service_status", unavailable)
    runtime.control(session.data, "touch")
    wait_until(lambda: state.read(session.path).get("health_status", {}).get("state") == "unavailable")
    ping = runtime.control(session.data, "ping")
    assert ping["ok"] and len(ping["health_status"]["error"]) > 4096
    assert runtime.is_running(session.data)
    assert ping["docker_status"]["verification"]["state"] == "verified"
    echo(session.port)


@pytest.mark.parametrize("session", [{}, {"published": 0}], indirect=True)
def test_health_read_outage_keeps_control_sync_and_forward_alive_then_recovers(session, monkeypatch):
    available = threading.Event()
    original = session.tunnel.process

    def health(*_):
        if not available.is_set():
            raise runtime.TransientDockerReadError("Docker status temporarily unavailable after EOF")
        return session.rows

    monkeypatch.setattr(runtime, "HEALTH_INTERVAL", 0)
    monkeypatch.setattr(runtime, "service_status", health)
    runtime.control(session.data, "touch")
    wait_until(lambda: state.read(session.path).get("health_status", {}).get("state") == "unavailable")
    stale = state.read(session.path)
    assert stale["status"] == "degraded"
    assert stale["services"][0]["State"] == "running"
    assert stale["health_status"]["last_success_at"] <= stale["health_status"]["checked_at"]
    assert stale["ports"][0]["status"] == "ready"
    assert runtime.control(session.data, "ping")["health_status"]["state"] == "unavailable"
    session.source.write_text("edit during health outage")
    wait_until(lambda: bool(session.sync.transfers) and
               session.sync.transfers[-1].get("podgrove-transfer/payload/source") == b"edit during health outage")
    echo(session.port)
    assert session.thread.is_alive() and session.tunnel.process is original
    session.sync.close.assert_not_called()
    session.api.close.assert_not_called()
    available.set()
    runtime.control(session.data, "touch")
    wait_until(lambda: state.read(session.path)["health_status"]["state"] == "ready")
    assert state.read(session.path)["status"] == "ready"
    assert "error" not in state.read(session.path)["health_status"]
    echo(session.port)


@pytest.mark.parametrize("session", [{"modes": ("normal", "fail")}], indirect=True)
def test_exhausted_forward_recovery_is_truthful_but_preserves_session_and_sync(session):
    first = session.tunnel.process
    first.terminate()
    first.wait(timeout=2)
    wait_until(lambda: state.read(session.path)["forward_status"]["state"] == "disconnected")
    observed = state.read(session.path)
    assert observed["status"] == "degraded"
    assert observed["ports"][0]["status"] == "disconnected"
    assert observed["ports"][0]["local"] == session.port
    assert runtime.control(session.data, "ping")["forward_status"]["state"] == "disconnected"
    assert session.thread.is_alive()
    session.api.close.assert_not_called()
    session.sync.close.assert_not_called()
    session.source.write_text("still synced without local endpoint")
    wait_until(lambda: len(session.sync.transfers) == 2)
    assert session.sync.transfers[-1]["podgrove-transfer/payload/source"] == b"still synced without local endpoint"


def test_forward_recovery_is_independent_of_slow_heartbeat(session):
    entered, release = threading.Event(), threading.Event()
    def heartbeat(*_):
        entered.set()
        assert release.wait(3)
    session.kube.heartbeat.side_effect = heartbeat
    runtime.control(session.data, "touch")
    assert entered.wait(2)
    try:
        first = session.tunnel.process
        first.terminate()
        first.wait(timeout=2)
        wait_until(lambda: state.read(session.path)["forward_status"]["state"] == "reconnecting")
        assert runtime.control(session.data, "ping")["status"] == "degraded"
        wait_until(lambda: session.tunnel.process is not first and session.tunnel.snapshot()["state"] == "ready", 2)
        echo(session.port)
    finally:
        release.set()


def test_heartbeat_outage_is_bounded_degraded_and_keeps_control_sync_and_forward(session, monkeypatch):
    import time
    from podgrove.kube import HeartbeatUnavailable
    available = threading.Event()
    attempts = []
    original_forward = session.tunnel.process

    def heartbeat(*_):
        attempts.append(time.monotonic())
        if not available.is_set():
            raise HeartbeatUnavailable("private-error-details-must-not-persist")

    monkeypatch.setattr(runtime, "HEARTBEAT_RETRY_DELAYS", (.8, .8))
    session.kube.heartbeat.side_effect = heartbeat
    runtime.control(session.data, "touch")
    wait_until(lambda: state.read(session.path).get("heartbeat_status", {}).get("state") == "unavailable")
    first = state.read(session.path)
    assert first["status"] == "degraded" and first["heartbeat_status"]["consecutive_failures"] == 1
    assert "private-error" not in str(first)
    ping = runtime.control(session.data, "ping")
    assert ping["ok"] and ping["heartbeat_status"]["state"] == "unavailable"
    assert observed(first, connected=True, ping=ping)["status"] == "degraded"
    echo(session.port)
    session.source.write_text("sync continues while heartbeat is unavailable")
    wait_until(lambda: bool(session.sync.transfers) and session.sync.transfers[-1].get("podgrove-transfer/payload/source") ==
               b"sync continues while heartbeat is unavailable")
    for _ in range(4):
        runtime.control(session.data, "touch")
        time.sleep(.02)
    wait_until(lambda: len(attempts) >= 2)
    assert attempts[1] - attempts[0] >= .75
    assert session.thread.is_alive() and session.tunnel.process is original_forward
    session.api.close.assert_not_called()
    session.sync.close.assert_not_called()
    available.set()
    wait_until(lambda: state.read(session.path)["heartbeat_status"]["state"] == "ready")
    recovered = state.read(session.path)
    assert recovered["status"] == "ready" and "error" not in recovered["heartbeat_status"]
    assert recovered["heartbeat_status"]["consecutive_failures"] == 0
    echo(session.port)


def test_confirmed_lease_ownership_failure_still_stops_session(session):
    from podgrove.errors import PodgroveError
    session.kube.heartbeat.side_effect = PodgroveError("Environment lease is missing or no longer owned")
    runtime.control(session.data, "touch")
    session.thread.join(timeout=5)
    assert not session.thread.is_alive()
    assert state.read(session.path)["status"] == "error"
    assert "no longer owned" in state.read(session.path)["error"]
    session.api.close.assert_called_once()
