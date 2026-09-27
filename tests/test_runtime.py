from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path
import os
import tempfile
import threading
import time
import uuid
import socket

import pytest

from podgrove import runtime
from podgrove import state as state_module
from podgrove.errors import PodgroveError
from podgrove.config import Config
from podgrove.process import docker_environment


@pytest.mark.parametrize("failure", ["connection reset by peer", "unexpected EOF", "EOF",
                                     "broken pipe", "docker timed out after 10s"])
def test_status_retries_a_transient_read_without_replaying_mutations(monkeypatch, tmp_path, failure):
    compose = Mock(config=SimpleNamespace(root=tmp_path))
    compose.command.return_value = ["docker", "compose", "ps", "--all", "--format", "json"]
    read = Mock(side_effect=[PodgroveError(failure),
                            SimpleNamespace(stdout='[{"Service":"api","State":"running"}]')])
    sleep = Mock()
    monkeypatch.setattr(runtime, "run", read)
    monkeypatch.setattr(runtime.time, "sleep", sleep)
    env = {"DOCKER_HOST": "tcp://127.0.0.1:43210"}
    assert runtime.service_status(compose, env) == [{"Service": "api", "State": "running"}]
    assert read.call_count == 2
    for call in read.call_args_list:
        assert call.args == (compose.command.return_value,)
        assert call.kwargs == {"env": env, "cwd": tmp_path, "timeout": runtime.STATUS_READ_TIMEOUT}
    assert all(call.args == ("ps", "--all", "--format", "json") for call in compose.command.call_args_list)
    sleep.assert_called_once_with(runtime.STATUS_RETRY_DELAYS[0])


def test_status_read_retry_exhaustion_is_bounded_and_identified_as_transient(monkeypatch, tmp_path):
    read = Mock(side_effect=PodgroveError("connection reset by peer"))
    sleep = Mock()
    monkeypatch.setattr(runtime, "run", read)
    monkeypatch.setattr(runtime.time, "sleep", sleep)
    with pytest.raises(runtime.TransientDockerReadError, match="temporarily unavailable after 3 read attempts"):
        runtime.service_status(Mock(config=SimpleNamespace(root=tmp_path)), {})
    assert read.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == list(runtime.STATUS_RETRY_DELAYS)


@pytest.mark.parametrize("failure", ["permission denied", "unknown flag", "invalid compose file"])
def test_status_does_not_retry_permanent_errors(monkeypatch, tmp_path, failure):
    read = Mock(side_effect=PodgroveError(failure))
    monkeypatch.setattr(runtime, "run", read)
    with pytest.raises(PodgroveError, match=failure):
        runtime.service_status(Mock(config=SimpleNamespace(root=tmp_path)), {})
    assert read.call_count == 1


def stack():
    return {"services": {
        "init": {"image": "busybox"},
        "api": {"image": "busybox", "depends_on": {"init": {"condition": "service_completed_successfully"}}},
        "disabled": {"image": "busybox", "deploy": {"replicas": 0}},
    }}


def test_readiness_accepts_successful_job_and_zero_replicas():
    assert runtime.readiness(stack(), [
        {"Service": "init", "State": "exited", "ExitCode": 0},
        {"Service": "api", "State": "running", "Health": "healthy"},
    ]) == (True, [])


@pytest.mark.parametrize("init", [
    {"Service": "init", "State": "exited", "ExitCode": 7},
    {"Service": "init", "State": "running"},
])
def test_readiness_requires_job_completion(init):
    ready, problems = runtime.readiness(stack(), [init, {"Service": "api", "State": "running"}])
    assert not ready
    assert any("init" in item for item in problems)


@pytest.mark.parametrize("state,health", [("running", "starting"), ("running", "unhealthy"), ("restarting", ""), ("exited", "")])
def test_readiness_rejects_unready_long_lived_services(state, health):
    ready, problems = runtime.readiness({"services": {"api": {}}}, [
        {"Service": "api", "State": state, "Health": health, "ExitCode": 0}
    ])
    assert not ready
    assert problems


def test_readiness_requires_all_replicas():
    ready, problems = runtime.readiness({"services": {"worker": {"deploy": {"replicas": 2}}}}, [
        {"Service": "worker", "State": "running"}
    ])
    assert not ready
    assert "1/2 containers present" in problems[0]


def test_initial_sync_must_finish_before_compose_up(monkeypatch, tmp_path):
    events = []
    sync = Mock()
    sync.start.side_effect = lambda: events.append("initial-sync")
    monkeypatch.setattr(runtime, "Synchronizer", Mock(return_value=sync))
    def run(*args, **kwargs):
        events.append("compose-up")
        return SimpleNamespace(stdout="", stderr="")
    monkeypatch.setattr(runtime, "run", run)
    monkeypatch.setattr(runtime, "service_status", lambda *_: [{"Service": "api", "State": "running"}])
    compose = Mock(config=SimpleNamespace(root=tmp_path))
    result, _, _ = runtime.launch_stack(compose, {"services": {"api": {}}}, {}, "123456abcdef")
    assert result is sync
    assert events == ["initial-sync", "compose-up"]


def test_initial_sync_failure_prevents_any_service_start(monkeypatch, tmp_path):
    sync = Mock()
    sync.start.side_effect = PodgroveError("missing required bind file")
    monkeypatch.setattr(runtime, "Synchronizer", Mock(return_value=sync))
    run = Mock()
    monkeypatch.setattr(runtime, "run", run)
    with pytest.raises(PodgroveError, match="missing required bind file"):
        runtime.launch_stack(Mock(config=SimpleNamespace(root=tmp_path)), {"services": {"api": {}}}, {}, "123456abcdef")
    run.assert_not_called()
    sync.close.assert_called_once()


def test_stack_cleanup_failure_never_replaces_primary_startup_error(monkeypatch, tmp_path, capsys):
    sync = Mock()
    sync.start.side_effect = PodgroveError("primary archive transfer failed")
    sync.close.side_effect = PodgroveError("secondary container inspect reset")
    monkeypatch.setattr(runtime, "Synchronizer", Mock(return_value=sync))
    with pytest.raises(PodgroveError, match="primary archive transfer failed"):
        runtime.launch_stack(Mock(config=SimpleNamespace(root=tmp_path)), {"services": {"api": {}}}, {}, "123456abcdef")
    assert "secondary container inspect reset" in capsys.readouterr().err


def test_failure_detail_includes_original_tunnel_cause_without_hiding_primary_error():
    tunnel = Mock()
    tunnel.check.side_effect = PodgroveError("Docker API exec transport exited 1: upstream disconnected")
    detail = runtime.failure_detail(PodgroveError("HTTP connection reset"), [tunnel, tunnel])
    assert detail.startswith("HTTP connection reset;")
    assert detail.count("upstream disconnected") == 1


def test_failed_container_surfaces_without_readiness_timeout(monkeypatch, tmp_path):
    sync = Mock()
    monkeypatch.setattr(runtime, "Synchronizer", Mock(return_value=sync))
    monkeypatch.setattr(runtime, "run", Mock(return_value=SimpleNamespace(stdout="", stderr="")))
    monkeypatch.setattr(runtime, "service_status", lambda *_: [{"Service": "api", "State": "exited", "ExitCode": 42}])
    with pytest.raises(PodgroveError, match="container failed"):
        runtime.launch_stack(Mock(config=SimpleNamespace(root=tmp_path)), {"services": {"api": {}}}, {}, "123456abcdef")
    sync.close.assert_called_once()


def test_docker_environment_does_not_reuse_context_tls_or_builder(monkeypatch):
    for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "BUILDX_BUILDER"):
        monkeypatch.setenv(key, "production-endpoint")
    monkeypatch.setenv("DOCKER_HOST", "tcp://unrelated:2375")
    result = docker_environment("tcp://127.0.0.1:12345")
    assert result["DOCKER_HOST"] == "tcp://127.0.0.1:12345"
    assert not any(key in result for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "BUILDX_BUILDER"))


@pytest.fixture
def supervised_session(tmp_path, monkeypatch, request):
    """Exercise the real supervisor and private socket with no Docker/cluster."""
    events = []
    fail_tunnel = threading.Event()
    fail_watch = threading.Event()
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    path = state_module.state_path(tmp_path, "test-context")
    token = uuid.uuid4().hex
    socket_path = Path(tempfile.gettempdir()) / f"podgrove-{os.getuid()}-{token[:16]}.sock"
    ident = state_module.identity(tmp_path)
    data = {"identity": ident, "root": str(tmp_path), "context": "test-context", "namespace": "podgrove-testing",
            "timeout": 10, "status": "starting", "token": token,
            "socket": str(socket_path), "ttl_seconds": 3600,
            "mr_url": getattr(request, "param", {}).get("mr_url", "")}
    settings = getattr(request, "param", {})
    data["namespace"] = settings.get("namespace", data["namespace"])
    if "namespace_mode" in settings:
        data["namespace_mode"] = settings["namespace_mode"]
    state_module.write(path, data)
    model = {"services": {"app": {"develop": {"watch": [{"path": str(tmp_path / "source.txt"),
                                                          "action": "sync", "target": "/app/source.txt"}]}}}}
    (tmp_path / "source.txt").write_text("source content\n")
    config = Config(root=tmp_path, files=[], ttl_seconds=3600)
    compose = Mock(config=config)
    compose.model.return_value = model
    compose.published_ports.return_value = []
    compose.has_watch.return_value = True
    compose.command.return_value = ["fake-docker", "compose", "watch"]
    sync = Mock()
    sync.sync_once.return_value = 0
    sync.close.side_effect = lambda: events.append("sync-close")
    kube = Mock()
    kube_factory = Mock(return_value=kube)
    monkeypatch.setattr(runtime, "Kube", kube_factory)
    monkeypatch.setattr(runtime, "load_config", lambda *_: config)
    monkeypatch.setattr(runtime, "Compose", Mock(return_value=compose))
    monkeypatch.setattr(runtime, "run", Mock(return_value=SimpleNamespace(stdout="", stderr="")))
    rows = [{"Service": "app", "State": "running", "Health": "healthy"}]
    monkeypatch.setattr(runtime, "launch_stack", lambda *_: (sync, 0.01, rows))
    monkeypatch.setattr(runtime, "service_status", lambda *_: rows)
    monkeypatch.setattr(runtime.signal, "signal", lambda *_: None)

    class FakeTunnel:
        def __init__(self, *args):
            pass

        def start(self):
            events.append("tunnel-start")
            return self

        def check(self):
            if fail_tunnel.is_set():
                raise PodgroveError("Kubernetes port-forward disconnected")

        def snapshot(self):
            return {"state": "ready", "error": None, "attempts": 0,
                    "changed_at": time.time(), "checked_at": time.time()}

        def close(self):
            events.append("tunnel-close")

    class FakeWatch:
        def __init__(self, *args, **kwargs):
            self.stopped = False
            events.append("watch-start")

        def poll(self):
            return -15 if self.stopped else (1 if fail_watch.is_set() else None)

        def terminate(self):
            events.append("watch-terminate")
            self.stopped = True

        def wait(self, **kwargs):
            return -15

    monkeypatch.setattr(runtime, "Tunnel", FakeTunnel)
    monkeypatch.setattr(runtime, "DockerTunnel", FakeTunnel)
    monkeypatch.setattr(runtime.subprocess, "Popen", FakeWatch)
    results = []
    thread = threading.Thread(target=lambda: results.append(runtime.serve(path)), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = state_module.read(path)
        if current["status"] in ("ready", "error"):
            break
        time.sleep(0.01)
    assert current["status"] == "ready", current
    session = SimpleNamespace(data=current, path=path, events=events, thread=thread,
                              results=results, fail_tunnel=fail_tunnel, fail_watch=fail_watch, kube=kube,
                              sync=sync, kube_factory=kube_factory)
    try:
        yield session
    finally:
        if thread.is_alive():
            try:
                runtime.control(current, "stop")
            except PodgroveError:
                pass
            thread.join(timeout=5)
        socket_path.unlink(missing_ok=True)
        assert not thread.is_alive(), "supervisor test did not shut down"


@pytest.mark.parametrize("supervised_session,expected", [
    ({"namespace": "wt-selected", "namespace_mode": "shared"}, "shared"),
    ({"namespace": "team-wt-123456abcdef", "namespace_mode": "worktree"}, "worktree"),
    ({"namespace": "wt-old"}, "exclusive"),
], indirect=["supervised_session"])
def test_supervisor_passes_saved_namespace_ownership_to_kube(supervised_session, expected):
    session = supervised_session
    session.kube_factory.assert_called_once_with("test-context", session.data["namespace"], namespace_mode=expected)


def test_supervisor_ready_touch_and_stop_use_private_socket(supervised_session):
    session = supervised_session
    ping = runtime.control(session.data, "ping")
    assert ping["ok"] is True and ping["status"] == "ready"
    assert ping["forward_status"]["state"] == "disabled" and ping["sync_status"]["state"] == "ready"
    wrong_token = {**session.data, "token": "not-the-token"}
    assert runtime.control(wrong_token, "stop") == {"ok": False}
    assert session.thread.is_alive()
    assert runtime.control(session.data, "touch")["ok"]
    assert runtime.control(session.data, "stop")["ok"]
    session.thread.join(timeout=5)
    assert session.results == [0]
    final = state_module.read(session.path)
    assert final["status"] == "disconnected"
    assert final["last_activity"] > session.data["last_activity"]
    assert not Path(session.data["socket"]).exists()
    assert session.events.index("watch-terminate") < session.events.index("sync-close")
    assert session.events.index("sync-close") < session.events.index("tunnel-close")
    session.kube.destroy.assert_not_called()


def test_supervisor_malformed_request_does_not_disconnect_session(supervised_session):
    session = supervised_session
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(session.data["socket"])
        client.sendall(b"[]\n")
        # Either an explicit rejection or closing this malformed connection is
        # fine, provided the valid authenticated session remains reachable.
        client.recv(4096)
    ping = runtime.control(session.data, "ping")
    assert ping["ok"] is True and ping["status"] == "ready"
    assert ping["forward_status"]["state"] == "disabled" and ping["sync_status"]["state"] == "ready"
    assert session.thread.is_alive()


@pytest.mark.parametrize("failure,error", [("fail_tunnel", "port-forward disconnected"),
                                          ("fail_watch", "compose watch exited")])
def test_supervisor_watch_and_tunnel_failures_publish_error_and_cleanup(supervised_session, failure, error):
    session = supervised_session
    getattr(session, failure).set()
    session.thread.join(timeout=5)
    assert session.results == [1]
    final = state_module.read(session.path)
    assert final["status"] == "error"
    assert error in final["error"]
    assert "sync-close" in session.events
    assert "tunnel-close" in session.events
    assert not Path(session.data["socket"]).exists()
    session.kube.destroy.assert_not_called()


def test_successful_supervisor_reap_removes_all_local_session_artifacts(supervised_session, monkeypatch):
    session = supervised_session
    session.path.with_suffix(".log").write_text("owned session log")
    session.path.with_suffix(".123.tmp").write_text("interrupted state write")
    monkeypatch.setattr(runtime, "reason", lambda *_args, **_kwargs: "idle TTL expired")
    (Path(session.data["root"]) / "source.txt").write_text("changed to trigger lifecycle check")
    session.thread.join(timeout=5)
    assert session.results == [0]
    session.kube.destroy.assert_called_once_with(session.data["identity"])
    assert not session.path.parent.exists()
    assert not Path(session.data["socket"]).exists()


def test_failed_supervisor_reap_preserves_state_and_log_for_retry(supervised_session, monkeypatch):
    session = supervised_session
    session.path.with_suffix(".log").write_text("diagnostic evidence")
    session.kube.destroy.side_effect = PodgroveError("cluster deletion denied")
    monkeypatch.setattr(runtime, "reason", lambda *_args, **_kwargs: "idle TTL expired")
    (Path(session.data["root"]) / "source.txt").write_text("changed to trigger lifecycle check")
    session.thread.join(timeout=5)
    saved = state_module.read(session.path)
    assert saved["status"] == "error" and "deletion denied" in saved["error"]
    assert session.path.with_suffix(".log").read_text() == "diagnostic evidence"
    assert not Path(session.data["socket"]).exists()
    assert not session.path.with_suffix(".lock").exists()


def test_stop_session_refuses_unreachable_existing_socket(tmp_path, monkeypatch):
    path = tmp_path / "bound.sock"
    path.touch()
    monkeypatch.setattr(runtime, "is_running", lambda _data: False)
    with pytest.raises(PodgroveError, match="cannot be authenticated"):
        runtime.stop_session({"socket": str(path)})
    assert path.exists()


def test_stop_session_waits_for_authenticated_shutdown(tmp_path, monkeypatch):
    path = tmp_path / "bound.sock"
    path.touch()
    monkeypatch.setattr(runtime, "is_running", lambda _data: True)
    def stop(_data, action):
        assert action == "stop"
        path.unlink()
        return {"ok": True}
    monkeypatch.setattr(runtime, "control", stop)
    runtime.stop_session({"socket": str(path)})
    assert not path.exists()


def test_stop_session_allows_provably_stale_owned_socket():
    path = Path(tempfile.gettempdir()) / f"pg-test-{uuid.uuid4().hex[:16]}.sock"
    try:
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
        runtime.stop_session({"socket": str(path), "token": uuid.uuid4().hex})
        assert path.exists(), "socket deletion happens only after successful cluster cleanup"
    finally:
        path.unlink(missing_ok=True)


def test_stop_session_refuses_unauthenticated_live_socket(monkeypatch):
    path = Path(tempfile.gettempdir()) / f"pg-test-{uuid.uuid4().hex[:16]}.sock"
    try:
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            server.listen(1)
            monkeypatch.setattr(runtime, "is_running", lambda _data: False)
            with pytest.raises(PodgroveError, match="cannot be authenticated"):
                runtime.stop_session({"socket": str(path), "token": uuid.uuid4().hex})
        assert path.exists()
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("slow_check", ["heartbeat", "service_status"])
def test_slow_maintenance_does_not_delay_serial_file_sync(supervised_session, monkeypatch, slow_check):
    session = supervised_session
    entered, release, delivered = threading.Event(), threading.Event(), threading.Event()
    source = Path(session.data["root"]) / "source.txt"
    parallel, maximum = 0, 0
    guard = threading.Lock()

    def slow(*_args):
        entered.set()
        assert release.wait(5), "Test did not release slow maintenance"
        return [{"Service": "app", "State": "running", "Health": "unhealthy"}]

    def sync_once():
        nonlocal parallel, maximum
        with guard:
            parallel += 1
            maximum = max(maximum, parallel)
        try:
            time.sleep(0.02)
            if source.read_text() == "edit during slow maintenance\n":
                delivered.set()
                return 1
            return 0
        finally:
            with guard:
                parallel -= 1

    session.sync.sync_once.side_effect = sync_once
    if slow_check == "heartbeat":
        session.kube.heartbeat.side_effect = slow
    else:
        monkeypatch.setattr(runtime, "HEALTH_INTERVAL", 0)
        monkeypatch.setattr(runtime, "service_status", slow)
    try:
        runtime.control(session.data, "touch")
        assert entered.wait(2)
        source.write_text("edit during slow maintenance\n")
        assert delivered.wait(1.5), "File sync was blocked behind unrelated maintenance"
        assert maximum == 1, "File transfers must never overlap"
    finally:
        release.set()
    if slow_check == "service_status":
        deadline = time.monotonic() + 2
        while state_module.read(session.path)["status"] == "ready" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert state_module.read(session.path)["status"] == "unhealthy"
        assert runtime.control(session.data, "ping")["status"] == "unhealthy"


def test_sync_error_during_slow_health_cannot_be_overwritten_as_ready(supervised_session, monkeypatch):
    session = supervised_session
    entered, release, failed = threading.Event(), threading.Event(), threading.Event()

    def health(*_):
        entered.set()
        assert release.wait(5)
        return [{"Service": "app", "State": "running", "Health": "healthy"}]

    def fail_sync():
        failed.set()
        raise PodgroveError("transfer failed; baseline retained")

    monkeypatch.setattr(runtime, "HEALTH_INTERVAL", 0)
    monkeypatch.setattr(runtime, "service_status", health)
    try:
        runtime.control(session.data, "touch")
        assert entered.wait(2)
        session.sync.sync_once.side_effect = fail_sync
        assert failed.wait(2)
    finally:
        release.set()
    session.thread.join(timeout=5)
    assert session.results == [1]
    result = state_module.read(session.path)
    assert result["status"] == "error"
    assert result["error"] == "transfer failed; baseline retained"
    assert session.events.index("sync-close") < session.events.index("tunnel-close")
    session.kube.destroy.assert_not_called()


def test_authenticated_stop_cancels_inflight_sync_before_helper_cleanup(supervised_session):
    session = supervised_session
    entered, cancelled = threading.Event(), threading.Event()

    def transfer():
        session.events.append("transfer-start")
        entered.set()
        assert cancelled.wait(5)
        session.events.append("transfer-finished")
        raise PodgroveError("cancelled owned transfer")

    def cancel():
        session.events.append("transfer-cancel")
        cancelled.set()

    session.sync.sync_once.side_effect = transfer
    session.sync.cancel.side_effect = cancel
    try:
        assert entered.wait(2)
        assert runtime.control(session.data, "stop")["ok"]
        session.thread.join(timeout=2)
        assert not session.thread.is_alive()
        assert session.results == [0]
        assert session.events.index("transfer-cancel") < session.events.index("transfer-finished")
        assert session.events.index("transfer-finished") < session.events.index("sync-close")
        assert state_module.read(session.path)["status"] == "disconnected"
    finally:
        cancelled.set()


def test_activity_during_slow_lifecycle_check_prevents_stale_ttl_reap(supervised_session, monkeypatch):
    session = supervised_session
    entered, release, delivered, rechecked = (threading.Event() for _ in range(4))
    observed = []
    source = Path(session.data["root"]) / "source.txt"

    def lifecycle(data, *, check_mr=True):
        observed.append(data["last_activity"])
        if len(observed) == 1:
            entered.set()
            assert release.wait(5)
            return "idle TTL expired"
        assert data["last_activity"] > observed[0], "Reaping must reread concurrent activity"
        rechecked.set()
        return None

    def transfer():
        if source.read_text() == "renew lease\n":
            delivered.set()
            return 1
        return 0

    session.sync.sync_once.side_effect = transfer
    monkeypatch.setattr(runtime, "reason", lifecycle)
    try:
        runtime.control(session.data, "touch")
        assert entered.wait(2)
        source.write_text("renew lease\n")
        assert delivered.wait(2)
    finally:
        release.set()
    assert rechecked.wait(2)
    assert session.thread.is_alive()
    session.kube.destroy.assert_not_called()


@pytest.mark.parametrize("supervised_session", [{"mr_url": "https://gitlab.com/example/project/-/merge_requests/1"}],
                         indirect=True)
def test_closed_mr_still_reaps_when_files_change_during_check(supervised_session, monkeypatch):
    session = supervised_session
    entered, release, delivered = (threading.Event() for _ in range(3))

    def lifecycle(data, *, check_mr=True):
        if not check_mr:
            return None
        entered.set()
        assert release.wait(5)
        return "merge request merged or closed"

    def transfer():
        if entered.is_set():
            delivered.set()
            return 1
        return 0

    session.sync.sync_once.side_effect = transfer
    monkeypatch.setattr(runtime, "reason", lifecycle)
    try:
        runtime.control(session.data, "touch")
        assert entered.wait(2)
        assert delivered.wait(2)
    finally:
        release.set()
    session.thread.join(timeout=5)
    assert session.results == [0]
    session.kube.destroy.assert_called_once_with(session.data["identity"])
    assert not session.path.exists()


def test_remote_build_output_hides_only_docker_desktop_navigation_line():
    original = ("#4 compiled successfully\n"
                "View build details: docker-desktop://dashboard/build/default/default/abc\n"
                "View build details: https://build.example.test/job/123\n"
                "error: cannot open docker-desktop://dashboard/build/default/default/abc\n")
    assert runtime._remote_build_output(original) == (
        "#4 compiled successfully\n"
        "View build details: https://build.example.test/job/123\n"
        "error: cannot open docker-desktop://dashboard/build/default/default/abc\n")
