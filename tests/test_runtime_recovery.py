"""Real supervisor/control sockets plus independent forwarding echo children."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from podgrove import runtime, state
from podgrove.config import Config
from podgrove.forward import Tunnel, free_port
from podgrove.sync import SnapshotRace, Synchronizer
from podgrove.sync_transport import TarStream
from podgrove.session_status import observed
from test_forward_recovery import LocalKube, echo, wait_until
from test_sync import FakeSynchronizer


class LocalStreamSynchronizer(FakeSynchronizer):
    """Real framed receiver process; Docker metadata/mutations remain inert."""

    def __init__(self, *args, **kwargs):
        self.receivers = []
        self.observed_container_id = "a" * 64
        super().__init__(*args, **kwargs)

    def _start_receiver(self, *, timeout=300):
        if self._receiver is not None:
            return
        nonce = uuid.uuid4().hex
        destination = self.root.parent / f"received-{len(self.receivers)}"
        destination.mkdir()
        program = r'''
import pathlib, sys
nonce, destination = sys.argv[1], pathlib.Path(sys.argv[2])
def exact(size):
    result = bytearray()
    while len(result) < size:
        chunk = sys.stdin.buffer.read(size - len(result))
        if not chunk: raise SystemExit(0)
        result.extend(chunk)
    return bytes(result)
print('READY ' + nonce, flush=True)
while True:
    magic, received_nonce, sequence, length = exact(72).decode().split()
    assert magic == 'PGS1' and received_nonce == nonce
    payload = exact(int(length))
    (destination / (sequence + '.tar')).write_bytes(payload)
    print('ACK ' + nonce + ' ' + sequence, flush=True)
'''
        self._receiver = TarStream([sys.executable, "-u", "-c", program, nonce, str(destination)],
                                   os.environ.copy(), self._cancelled, nonce, timeout=min(5, timeout))
        self.receivers.append(self._receiver)
        self._receiver.ready()

    def _send_archive(self, archive):
        Synchronizer._send_archive(self, archive)
        archive.seek(0)
        FakeSynchronizer._send_archive(self, archive)

    def _docker(self, *args, **kwargs):
        if len(args) > 3 and args[1:4] == ("inspect", "--format", "{{json .}}"):
            self.calls.append(args)
            if args[0] == "container":
                value = {"Id": self.observed_container_id, "Name": "/" + self.container,
                         "State": {"Running": True}, "Config": {"Labels": {} if self.foreign else self.labels},
                         "Mounts": [{"Type": "bind", "Source": str(self.root), "Destination": "/workspace", "RW": True},
                                    {"Type": "volume", "Name": self.volume, "Destination": "/metadata", "RW": True}]}
            else:
                value = {"Name": self.volume, "Labels": self.labels}
            return subprocess.CompletedProcess(args, 0, json.dumps(value).encode(), b"")
        if args[:1] == ("exec",) and args[-2:] == ("cat", "/metadata/baseline.json"):
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0, self.remote_baseline, b"")
        return super()._docker(*args, **kwargs)


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
    monkeypatch.setattr(runtime, "SYNC_RETRY_DELAYS", settings.get("sync_delays", (.25, .1, .1)))
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
    original_call = kube.call
    def read(*args, **kwargs):
        if kwargs.get("check") is False:
            assert args[0] == "get" and args[1] in ("statefulset", "pod")
            assert 0 < kwargs["timeout"] <= 15 and isinstance(kwargs["cancel_event"], threading.Event)
            kube.reads.append(args)
            value = kube.controller if args[1] == "statefulset" else kube.pod
            return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
        return original_call(*args, **kwargs)
    kube.call = read
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
    sync_type = LocalStreamSynchronizer if settings.get("sync_stream") else FakeSynchronizer
    sync = sync_type(root, [source], identity=ident)
    sync.start()
    sync_close = Mock(wraps=sync.close)
    sync.close = sync_close
    launch = Mock(return_value=(sync, .01, rows))
    monkeypatch.setattr(runtime, "launch_stack", launch)
    monkeypatch.setattr(runtime, "service_status", lambda *_: rows)
    api = Mock()
    api.start.return_value = api
    api.snapshot.return_value = {"verification": {"state": "verified"}, "failed_connections": 0}
    api.identity_snapshot.return_value = {"state": "verified", "expected": {
        "statefulset_uid": kube.controller["metadata"]["uid"], "pod_uid": kube.pod["metadata"]["uid"]}}
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
                              tunnel=tunnels[0], port=port, thread=thread, result=result, rows=rows, launch=launch)
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
        for receiver in getattr(sync, "receivers", []):
            assert receiver.process.poll() is not None and not receiver._reader.is_alive()


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


@pytest.mark.parametrize("session", [{"sync_stream": True}], indirect=True)
def test_idle_sync_exit_recovers_and_next_real_frame_preserves_session(session, monkeypatch):
    from podgrove.sync_recovery import SyncRecoveryUnavailable
    entered, release = threading.Event(), threading.Event()
    original_guard = session.sync.reconnect_guard
    def guard(cancelled, **kwargs):
        entered.set()
        while not release.wait(.01):
            if cancelled.is_set():
                raise SyncRecoveryUnavailable("cancelled")
        return original_guard(cancelled, **kwargs)
    monkeypatch.setattr(session.sync, "reconnect_guard", guard)
    original_forward = session.tunnel.process
    first = session.sync._receiver
    baseline = session.sync._baseline.copy()
    first.process.terminate()
    first.process.wait(timeout=2)
    try:
        assert entered.wait(4)
        ping = runtime.control(session.data, "ping")
        assert ping["status"] == "degraded" and ping["sync_status"]["attempts"] == 1
        assert observed(state.read(session.path), connected=True, ping=ping)["status"] == "degraded"
        assert session.sync._baseline == baseline and len(session.sync.transfers) == 1
        session.source.write_text("edit while idle transport reconnects")
        echo(session.port)
    finally:
        release.set()
    wait_until(lambda: state.read(session.path)["sync_status"]["state"] == "ready")
    assert session.sync._receiver is not first and first.process.poll() is not None
    assert not first._reader.is_alive()
    assert len(session.sync.receivers) == 2
    assert session.sync.transfers[-1]["podgrove-transfer/payload/source"] == b"edit while idle transport reconnects"
    assert len(session.sync.transfers) == 2
    assert len(list(session.source.parent.parent.glob("received-*/*.tar"))) == 2
    assert [call for call in session.sync.calls if call[0] == "restart"] == [("restart", "-t", "2", "a" * 64)]
    assert session.tunnel.process is original_forward and session.thread.is_alive()
    session.launch.assert_called_once()
    session.api.close.assert_not_called()
    session.sync.close.assert_not_called()
    assert state.read(session.path)["status"] == "ready"
    echo(session.port)


@pytest.mark.parametrize("session", [{"sync_stream": True}], indirect=True)
def test_sync_ownership_read_outage_retries_without_restarting_until_verified(session, monkeypatch):
    from podgrove.sync_recovery import SyncRecoveryUnavailable
    available = threading.Event()
    entered = threading.Event()
    original = session.sync.reconnect_guard
    checks = []
    def guard(*args, **kwargs):
        checks.append(None)
        if len(checks) == 1:
            raise SyncRecoveryUnavailable("read unavailable")
        entered.set()
        while not available.wait(.01):
            if args[0].is_set():
                raise SyncRecoveryUnavailable("cancelled")
        return original(*args, **kwargs)
    monkeypatch.setattr(session.sync, "reconnect_guard", guard)
    session.sync._receiver.process.terminate()
    session.sync._receiver.process.wait(timeout=2)
    try:
        assert entered.wait(4)
        assert not any(call[0] == "restart" for call in session.sync.calls)
        assert runtime.control(session.data, "ping")["sync_status"]["state"] == "reconnecting"
        echo(session.port)
    finally:
        available.set()
    wait_until(lambda: state.read(session.path)["sync_status"]["state"] == "ready")
    assert len(session.sync.receivers) == 2 and len(session.sync.transfers) == 1
    session.launch.assert_called_once()


@pytest.mark.parametrize("session", [{"sync_stream": True}], indirect=True)
def test_exhausted_sync_recovery_pauses_only_sync_with_bounded_attempts(session, monkeypatch):
    from podgrove.sync_recovery import SyncRecoveryUnavailable
    guard = Mock(side_effect=SyncRecoveryUnavailable("unavailable"))
    monkeypatch.setattr(session.sync, "reconnect_guard", guard)
    first_forward = session.tunnel.process
    session.sync._receiver.process.terminate()
    session.sync._receiver.process.wait(timeout=2)
    wait_until(lambda: state.read(session.path)["sync_status"]["state"] == "disconnected")
    value = state.read(session.path)
    assert value["status"] == "degraded" and value["sync_status"]["attempts"] == 3
    assert "up --refresh" in value["sync_status"]["error"]
    assert runtime.control(session.data, "ping")["ok"]
    assert session.thread.is_alive() and session.tunnel.process is first_forward
    session.source.write_text("paused edit must not transfer")
    runtime.control(session.data, "touch")
    echo(session.port)
    assert guard.call_count == 3 and len(session.sync.receivers) == 1
    assert len(session.sync.transfers) == 1
    assert not any(call[0] == "restart" for call in session.sync.calls)
    session.launch.assert_called_once()


@pytest.mark.parametrize("session", [{"sync_stream": True}], indirect=True)
@pytest.mark.parametrize("failure", ["helper", "labels", "mount", "volume", "baseline", "restart"])
def test_sync_recovery_refuses_helper_drift_or_uncertain_restart_without_compose_replay(session, monkeypatch, failure):
    from podgrove.errors import PodgroveError
    if failure == "helper":
        session.sync.observed_container_id = "b" * 64
    elif failure == "labels":
        session.sync.foreign = True
    elif failure == "baseline":
        baseline = json.loads(session.sync.remote_baseline)
        baseline["entries"]["source"]["digest"] = "different"
        session.sync.remote_baseline = json.dumps(baseline).encode()
    elif failure in ("mount", "volume"):
        original = session.sync._docker
        def altered(*args, **kwargs):
            result = original(*args, **kwargs)
            if len(args) > 3 and args[1:4] == ("inspect", "--format", "{{json .}}"):
                value = json.loads(result.stdout)
                if failure == "mount" and args[0] == "container":
                    value["Mounts"][0]["Source"] = "/different-worktree"
                elif failure == "volume" and args[0] == "volume":
                    value["Labels"] = {}
                return subprocess.CompletedProcess(args, 0, json.dumps(value).encode(), b"")
            return result
        monkeypatch.setattr(session.sync, "_docker", altered)
    else:
        original = session.sync._docker
        def ambiguous(*args, **kwargs):
            if args[0] == "restart":
                session.sync.calls.append(args)
                raise PodgroveError("restart reply lost")
            return original(*args, **kwargs)
        monkeypatch.setattr(session.sync, "_docker", ambiguous)
    session.sync._receiver.process.terminate()
    session.sync._receiver.process.wait(timeout=2)
    wait_until(lambda: state.read(session.path)["sync_status"]["state"] == "disconnected")
    assert state.read(session.path)["status"] == "degraded"
    assert runtime.control(session.data, "ping")["ok"]
    assert len(session.sync.receivers) == 1 and len(session.sync.transfers) == 1
    assert len([call for call in session.sync.calls if call[0] == "restart"]) == int(failure in ("baseline", "restart"))
    session.launch.assert_called_once()
    echo(session.port)
    session.sync.foreign = False  # Allow inert fixture cleanup after the refusal.


@pytest.mark.parametrize("session", [{"sync_stream": True}], indirect=True)
def test_confirmed_sync_engine_replacement_still_stops_session_before_helper_restart(session):
    session.kube.pod["metadata"]["uid"] = "replacement-pod"
    session.sync._receiver.process.terminate()
    session.sync._receiver.process.wait(timeout=2)
    session.thread.join(timeout=5)
    assert not session.thread.is_alive() and state.read(session.path)["status"] == "error"
    assert "replaced" in state.read(session.path)["error"]
    assert not any(call[0] == "restart" for call in session.sync.calls)
    session.launch.assert_called_once()


@pytest.mark.parametrize("session", [{"sync_stream": True}], indirect=True)
def test_uncertain_sync_batch_pauses_without_reconnect_and_keeps_control_forwarding(session, monkeypatch):
    from podgrove.sync_transport import SyncStreamError
    original = session.sync._send_archive
    calls = []
    def lost_ack(archive):
        original(archive)  # Model a remote commit whose response becomes lost.
        calls.append(None)
        raise SyncStreamError("batch completion is uncertain", reconnectable=False)
    monkeypatch.setattr(session.sync, "_send_archive", lost_ack)
    baseline = session.sync._baseline.copy()
    session.source.write_text("commit without trustworthy acknowledgement")
    wait_until(lambda: state.read(session.path)["sync_status"]["state"] == "disconnected")
    assert session.sync._baseline == baseline and len(calls) == 1
    assert len(session.sync.receivers) == 1 and not any(call[0] == "restart" for call in session.sync.calls)
    assert state.read(session.path)["status"] == "degraded"
    assert runtime.control(session.data, "ping")["ok"]
    echo(session.port)
    session.launch.assert_called_once()


@pytest.mark.parametrize("session", [{"sync_stream": True}], indirect=True)
def test_sync_stop_cancels_pending_ownership_read_before_join(session, monkeypatch):
    from podgrove.sync_recovery import SyncRecoveryUnavailable
    entered = threading.Event()
    def blocked(cancelled, **_kwargs):
        entered.set()
        assert cancelled.wait(3)
        raise SyncRecoveryUnavailable("cancelled")
    monkeypatch.setattr(session.sync, "reconnect_guard", blocked)
    session.sync._receiver.process.terminate()
    session.sync._receiver.process.wait(timeout=2)
    assert entered.wait(3)
    assert runtime.control(session.data, "stop")["ok"]
    session.thread.join(timeout=2)
    assert not session.thread.is_alive()
    assert state.read(session.path)["status"] == "disconnected"
    assert not any(call[0] == "restart" for call in session.sync.calls)


@pytest.mark.parametrize("session", [{"sync_stream": True}], indirect=True)
def test_lost_baseline_read_retries_observation_without_repeating_acknowledged_restart(session, monkeypatch):
    from podgrove.sync_recovery import SyncRecoveryUnavailable
    original = session.sync._verify_baseline
    calls = []
    def baseline():
        calls.append(None)
        if len(calls) == 1:
            raise SyncRecoveryUnavailable("baseline read reset")
        return original()
    monkeypatch.setattr(session.sync, "_verify_baseline", baseline)
    session.sync._receiver.process.terminate()
    session.sync._receiver.process.wait(timeout=2)
    wait_until(lambda: len(session.sync.receivers) == 2 and state.read(session.path)["sync_status"]["state"] == "ready")
    assert len(calls) == 2
    assert len([call for call in session.sync.calls if call[0] == "restart"]) == 1
    assert len(session.sync.transfers) == 1
    echo(session.port)
