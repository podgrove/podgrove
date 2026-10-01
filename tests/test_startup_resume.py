"""Partial startup retains diagnostics; explicit re-up remirrors before restart."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from podgrove import runtime, state
from podgrove.compose import Compose
from podgrove.config import Config
from podgrove.errors import PodgroveError


@pytest.mark.parametrize("failed_state", ["exited", "unhealthy"])
def test_file_appearing_after_failure_is_mirrored_before_failed_service_recreation(tmp_path, monkeypatch, failed_state):
    source, remote = tmp_path / "source", tmp_path / "mirror"
    source.mkdir()
    events, syncers = [], []
    rows = [{"Service": "healthy", "Project": "fixture", "State": "running", "Health": "healthy"}]
    model = {"name": "fixture", "services": {"app": {}, "healthy": {}}}
    compose = Compose(Config(source, []))
    compose.recover_existing = True

    class Mirror:
        closed = False

        def __init__(self, *_args, **_kwargs):
            syncers.append(self)

        def start(self):
            shutil.copytree(source, remote, dirs_exist_ok=True)
            events.append("mirror")

        def close(self):
            self.closed = True

    def launch(command, **_kwargs):
        events.append(tuple(command[command.index("up"):]))
        result = subprocess.run([sys.executable, "-c", "from pathlib import Path;import sys;sys.exit(0 if Path(sys.argv[1]).is_file() else 7)",
                                 str(remote / "required.txt")], capture_output=True, text=True, timeout=5)
        rows[:] = [rows[0], {"Service": "app", "Project": "fixture",
                             "State": "running" if result.returncode == 0 else "exited", "ExitCode": result.returncode}]
        if result.returncode:
            raise PodgroveError("startup requires a file that has not arrived")
        return result

    monkeypatch.setattr(runtime, "Synchronizer", Mirror)
    monkeypatch.setattr(runtime, "run", launch)
    monkeypatch.setattr(runtime, "service_status", lambda *_, **_kw: list(rows))
    with pytest.raises(runtime.StartupIncomplete) as failed:
        runtime.launch_stack(compose, model, {}, "012345abcdef", 5)
    assert failed.value.sync is syncers[0] and not syncers[0].closed
    assert failed.value.rows[0]["State"] == "running"
    failed.value.sync.close()
    if failed_state == "unhealthy":
        rows[1].update(State="running", Health="unhealthy")
    (source / "required.txt").write_text("appeared\n")
    recovered, _, observed = runtime.launch_stack(compose, model, {}, "012345abcdef", 5)
    assert recovered is syncers[1] and not recovered.closed
    assert runtime.readiness(model, observed) == (True, [])
    assert events == ["mirror", ("up", "--detach", "--build"), "mirror",
                      ("up", "--detach", "--build", "--force-recreate", "--no-deps", "app"),
                      ("up", "--detach", "--build")]
    assert (remote / "required.txt").read_text() == "appeared\n"


def test_successful_completion_jobs_are_not_force_recreated(monkeypatch, tmp_path):
    compose = Compose(Config(tmp_path, []))
    compose.recover_existing = True
    model = {"services": {"job": {}, "app": {"depends_on": {"job": {"condition": "service_completed_successfully"}}}}}
    rows = [{"Service": "job", "State": "exited", "ExitCode": 0}, {"Service": "app", "State": "running"}]
    monkeypatch.setattr(runtime, "Synchronizer", Mock())
    monkeypatch.setattr(runtime, "service_status", lambda *_, **_kw: rows)
    run = Mock(return_value=SimpleNamespace(stdout="", stderr=""))
    monkeypatch.setattr(runtime, "run", run)
    runtime.launch_stack(compose, model, {}, "012345abcdef")
    assert len(run.call_args_list) == 1
    assert "--force-recreate" not in run.call_args.args[0]


@pytest.mark.parametrize("health", ["healthy", "starting", "unhealthy"])
def test_partial_port_selection_keeps_running_services_and_refuses_foreign_publishers(tmp_path, health):
    compose = Compose(Config(tmp_path, []))
    model = {"name": "fixture", "services": {name: {"ports": [{"target": 80, "published": 0}]}
                                             for name in ("healthy", "failed")}}
    rows = [{"Service": "healthy", "Project": "fixture", "State": "running", "Health": health,
             "Publishers": [{"TargetPort": 80, "PublishedPort": 32123, "Protocol": "tcp", "URL": "0.0.0.0"}]},
            {"Service": "failed", "Project": "fixture", "State": "exited", "ExitCode": 7}]
    ports = runtime.partial_port_plan(compose, model, rows, "012345abcdef")
    assert len(ports) == 1 and ports[0]["service"] == "healthy" and ports[0]["published"] == 32123
    rows[0]["Project"] = "another-project"
    with pytest.raises(PodgroveError, match="different Compose project"):
        runtime.partial_port_plan(compose, model, rows, "012345abcdef")


def test_partial_startup_keeps_control_sync_and_docker_for_diagnosis(tmp_path, monkeypatch):
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    ident = state.identity(tmp_path)
    path = state.state_path(tmp_path, "test-context")
    socket_path = Path("/tmp") / f"pg-partial-{time.time_ns()}.sock"
    data = {"identity": ident, "root": str(tmp_path), "context": "test-context", "namespace": "test-namespace",
            "timeout": 5, "status": "starting", "token": "inert", "socket": str(socket_path), "ttl_seconds": 3600}
    state.write(path, data)
    config = Config(tmp_path, [])
    compose = Compose(config)
    model = {"services": {"app": {}}}
    monkeypatch.setattr(compose, "model", lambda: model)
    monkeypatch.setattr(runtime, "Compose", lambda *_: compose)
    monkeypatch.setattr(runtime, "load_config", lambda *_: config)
    monkeypatch.setattr(runtime, "Kube", Mock())
    monkeypatch.setattr(runtime.signal, "signal", lambda *_: None)
    sync = Mock()
    sync.sync_once.return_value = 0
    rows = [{"Service": "app", "State": "exited", "ExitCode": 7}]
    monkeypatch.setattr(runtime, "launch_stack", Mock(side_effect=runtime.StartupIncomplete("missing source file", sync, .1, rows)))
    monkeypatch.setattr(runtime, "run", Mock())
    monkeypatch.setattr(runtime, "service_status", lambda *_, **_kw: rows)
    api = Mock()
    api.start.return_value = api
    api.snapshot.return_value = {"verification": {"state": "verified"}}
    api.identity_snapshot.return_value = {}
    monkeypatch.setattr(runtime, "DockerTunnel", Mock(return_value=api))
    results = []
    worker = threading.Thread(target=lambda: results.append(runtime.serve(path)), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while state.read(path)["status"] == "starting" and time.monotonic() < deadline:
            time.sleep(.01)
        observed = state.read(path)
        assert observed["status"] == "unhealthy" and observed["startup_status"]["state"] == "failed"
        assert observed["docker_host"].startswith("tcp://127.0.0.1:")
        assert runtime.control(data, "ping")["status"] == "unhealthy"
        deadline = time.monotonic() + 2
        while not sync.sync_once.called and time.monotonic() < deadline:
            time.sleep(.01)
        assert sync.sync_once.called
        sync.close.assert_not_called()
        api.close.assert_not_called()
        assert runtime.control(data, "stop")["ok"]
        worker.join(5)
        assert results == [0]
        sync.close.assert_called_once()
        api.close.assert_called_once()
    finally:
        if worker.is_alive():
            runtime.control(data, "stop")
            worker.join(5)
        socket_path.unlink(missing_ok=True)
        assert not worker.is_alive()


@pytest.mark.parametrize("failure", ["ordinary", "ownership", "identity", "cancel", "deadline", "healthy"])
def test_initial_forward_failure_retains_only_verified_partial_startup(tmp_path, monkeypatch, capsys, failure):
    from podgrove import cli, exec_transport
    from podgrove.forward import ForwardOwnershipError, Tunnel
    from test_forward_recovery import IDENT, LocalKube, wait_until

    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    ident = state.identity(tmp_path)
    path = state.state_path(tmp_path, "test-context")
    socket_path = Path("/tmp") / f"pg-partial-forward-{time.time_ns()}.sock"
    data = {"identity": ident, "root": str(tmp_path), "context": "test-context", "namespace": "test-namespace",
            "timeout": 5, "status": "starting", "token": "inert", "socket": str(socket_path), "ttl_seconds": 3600,
            "compose_project": "fixture", "compose_services": ["healthy", "failed"]}
    state.write(path, data)
    config = Config(tmp_path, [])
    compose = Compose(config)
    model = {"name": "fixture", "services": {"healthy": {"ports": [{"target": 80, "published": 8080}]}, "failed": {}}}
    rows = [{"Service": "healthy", "Project": "fixture", "State": "running", "Health": "healthy",
             "Publishers": [{"TargetPort": 80, "PublishedPort": 8080, "Protocol": "tcp", "URL": "0.0.0.0"}]},
            {"Service": "failed", "Project": "fixture", "State": "exited", "ExitCode": 7}]
    if failure == "healthy":
        rows[1].update(State="running", ExitCode=0)
    monkeypatch.setattr(compose, "model", lambda: model)
    for module in (runtime, cli):
        monkeypatch.setattr(module, "Compose", lambda *_: compose)
        monkeypatch.setattr(module, "load_config", lambda *_: config)
        monkeypatch.setattr(module, "Kube", Mock())
    monkeypatch.setattr(runtime.signal, "signal", lambda *_: None)
    sync = Mock()
    sync.sync_once.return_value = 0
    primary = "Compose startup failed: required source file is missing"
    launch = Mock(return_value=(sync, .1, rows), side_effect=None if failure == "healthy" else
                  runtime.StartupIncomplete(primary, sync, .1, rows))
    monkeypatch.setattr(runtime, "launch_stack", launch)
    monkeypatch.setattr(runtime, "run", Mock())
    monkeypatch.setattr(runtime, "service_status", lambda *_, **_kw: rows)
    offset = [0]
    monkeypatch.setattr(runtime, "time", SimpleNamespace(time=time.time, sleep=time.sleep,
                                                        monotonic=lambda: time.monotonic() + offset[0]))
    api = Mock()
    api.start.return_value = api
    api.snapshot.return_value = {"verification": {"state": "verified"}}
    api.identity_snapshot.return_value = {"expected": {"statefulset_uid": "original-controller", "pod_uid": "original-pod"}}
    monkeypatch.setattr(runtime, "DockerTunnel", Mock(return_value=api))
    forwards = []

    def forward(_kube, _ident, ports):
        tunnel = Tunnel(LocalKube(tmp_path / "forward-control", modes=("fail",)), IDENT, ports)
        start = tunnel.start

        def failing_start():
            if failure != "healthy":
                saved = state.read(path)
                assert saved["startup_status"]["error"] == saved["error"] == primary
            if failure == "ownership":
                raise ForwardOwnershipError("Application engine ownership changed")
            try:
                return start(timeout=2)
            except PodgroveError:
                if failure == "identity":
                    api.refresh_identity.side_effect = PodgroveError("Pinned engine verification failed")
                elif failure == "cancel":
                    assert runtime.control(data, "stop")["ok"]
                elif failure == "deadline":
                    offset[0] = 1000
                raise

        tunnel.start = failing_start
        tunnel.close = Mock(wraps=tunnel.close)
        forwards.append(tunnel)
        return tunnel

    monkeypatch.setattr(runtime, "Tunnel", forward)
    results = []
    worker = threading.Thread(target=lambda: results.append(runtime.serve(path)), daemon=True)
    worker.start()
    try:
        wait_until(lambda: state.read(path)["status"] != "starting")
        observed = state.read(path)
        if failure == "ordinary":
            assert observed["status"] == "degraded"
            assert observed["startup_status"]["state"] == "failed"
            assert observed["startup_status"]["error"] == observed["error"] == primary
            assert observed["forward_status"]["state"] == "disconnected"
            assert "simulated forwarding failure" in observed["forward_status"]["error"]
            assert len(observed["ports"]) == 1 and observed["ports"][0]["status"] == "disconnected"
            assert runtime.control(data, "ping")["forward_status"]["state"] == "disconnected"
            wait_until(lambda: sync.sync_once.called)
            api.close.assert_not_called()
            sync.close.assert_not_called()
            assert api.refresh_identity.call_count == 2

            def project_paths(args, _root=None):
                args._config_root = tmp_path
                return tmp_path

            monkeypatch.setattr(cli, "_project_paths", project_paths)
            flags = ["--project-directory", str(tmp_path), "--context", data["context"], "--namespace", data["namespace"]]
            capsys.readouterr()
            assert cli.execute(cli.parser().parse_args(["status", *flags, "--json"])) == 1
            report = json.loads(capsys.readouterr().out)
            assert report["error"] == primary and report["forward_status"]["state"] == "disconnected"
            execution = Mock(return_value=7)
            monkeypatch.setattr(exec_transport, "run_exec", execution)
            assert cli.execute(cli.parser().parse_args(["exec", *flags, "healthy", "--", "true"])) == 7
            assert execution.call_count == 1 and execution.call_args.args[1:5] == (ident, "fixture", "healthy", ["true"])
            assert runtime.control(data, "stop")["ok"]
        else:
            assert observed["status"] == "error"
            if failure != "healthy":
                assert observed["startup_status"]["error"] == primary
                assert observed["error"].startswith(primary)
            expected = {"ownership": "ownership changed", "identity": "Pinned engine verification failed",
                        "cancel": "startup cancelled", "deadline": "deadline expired",
                        "healthy": "simulated forwarding failure"}[failure]
            assert expected in observed["error"]
        worker.join(5)
        assert results == [0 if failure == "ordinary" else 1]
        assert launch.call_count == 1
        api.close.assert_called_once()
        sync.close.assert_called_once()
        assert len(forwards) == 1
        forwards[0].close.assert_called_once()
        assert forwards[0].process is None or forwards[0].process.poll() is not None
        assert forwards[0].log is None and not socket_path.exists()
    finally:
        if worker.is_alive():
            runtime.control(data, "stop")
            worker.join(5)
        socket_path.unlink(missing_ok=True)
        assert not worker.is_alive()


@pytest.mark.parametrize("change,failures,expected_attempts", [("pod", 1, 2), ("pod", 3, 3),
                                                             ("pvc", 1, 1), ("statefulset", 1, 1),
                                                             ("pod-before-api", 0, 1)])
def test_only_startup_replays_after_owned_pod_replacement(tmp_path, monkeypatch, change, failures, expected_attempts):
    from copy import deepcopy
    from podgrove.startup_recovery import capture_anchor
    from test_startup_recovery import Kube as FixtureKube, IDENT
    from podgrove.kube import ENVIRONMENT

    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    ident = state.identity(tmp_path)
    kube = FixtureKube()
    for item in kube.objects.values():
        item["metadata"]["name"] = item["metadata"]["name"].replace(IDENT, ident)
        item["metadata"]["labels"][ENVIRONMENT] = ident
    kube.objects["pod"]["metadata"]["ownerReferences"][0]["name"] = "pg-" + ident
    kube.objects["pod"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "pg-" + ident
    kube.heartbeat, kube.destroy = Mock(), Mock()
    path = state.state_path(tmp_path, kube.context)
    socket_path = Path("/tmp") / f"pg-retry-{time.time_ns()}.sock"
    record = {"identity": ident, "root": str(tmp_path), "context": kube.context, "namespace": kube.namespace,
              "timeout": 4, "status": "starting", "token": "inert", "socket": str(socket_path),
              "ttl_seconds": 3600, "startup_anchor": capture_anchor(kube, ident)}
    state.write(path, record)
    config = Config(tmp_path, [])
    compose = Compose(config)
    model = {"services": {"app": {}}}
    monkeypatch.setattr(compose, "model", lambda: model)
    monkeypatch.setattr(runtime, "Compose", lambda *_: compose)
    monkeypatch.setattr(runtime, "load_config", lambda *_: config)
    monkeypatch.setattr(runtime, "Kube", lambda *_a, **_kw: kube)
    monkeypatch.setattr(runtime.signal, "signal", lambda *_: None)
    syncs, tunnels, commands, info_calls = [], [], [], []

    def synchronizer(*_a, **_kw):
        sync = Mock()
        sync.sync_once.return_value = 0
        syncs.append(sync)
        return sync

    class API:
        def __init__(self, *_a):
            if change == "pod-before-api" and not tunnels:
                kube.objects["pod"]["metadata"]["uid"] = "api-race-replacement"
            self.expected = {"statefulset_uid": kube.objects["statefulset"]["metadata"]["uid"],
                             "pod_uid": kube.objects["pod"]["metadata"]["uid"]}
            self.closed = False
            tunnels.append(self)

        def start(self):
            return self

        def check(self):
            self.refresh_identity()

        def identity_snapshot(self):
            return {"state": "verified", "expected": deepcopy(self.expected), "observed": deepcopy(self.expected)}

        def refresh_identity(self):
            current = (kube.objects["statefulset"]["metadata"]["uid"], kube.objects["pod"]["metadata"]["uid"])
            if current != (self.expected["statefulset_uid"], self.expected["pod_uid"]):
                raise runtime.EngineReplacedError(tuple(self.expected.values()), current)
            return self.identity_snapshot()

        def snapshot(self):
            return {"verification": {"state": "verified"}}

        def close(self):
            self.closed = True

    def run(command, **_kw):
        if command == ["docker", "info"]:
            info_calls.append(command)
            return SimpleNamespace(stdout="", stderr="")
        commands.append(command)
        assert command[-3:] == ["up", "--detach", "--build"]
        if len(commands) <= failures:
            kube.objects[change]["metadata"]["uid"] = f"replacement-{len(commands)}"
            raise PodgroveError("build connection interrupted")
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(runtime, "Synchronizer", synchronizer)
    monkeypatch.setattr(runtime, "DockerTunnel", API)
    monkeypatch.setattr(runtime, "run", run)
    monkeypatch.setattr(runtime, "service_status", lambda *_, **_kw: [{"Service": "app", "State": "running"}])
    results = []
    worker = threading.Thread(target=lambda: results.append(runtime.serve(path)), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 12
        while state.read(path)["status"] == "starting" and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        observed = state.read(path)
        assert len(commands) == expected_attempts
        assert len(info_calls) == expected_attempts
        if change == "pod" and failures == 1 or change == "pod-before-api":
            assert observed["status"] == "ready", observed
            assert observed["startup_status"]["attempts"] == 1
            assert runtime.control(record, "ping")["ok"]
            assert runtime.control(record, "stop")["ok"]
            worker.join(5)
            assert results == [0]
        else:
            worker.join(5)
            assert results == [1] and observed["status"] == "error", observed
            assert "exhausted" in observed["error"] if change == "pod" else "refusing replay" in observed["error"]
        assert all(tunnel.closed for tunnel in tunnels)
        assert all(sync.close.called for sync in syncs)
        kube.destroy.assert_not_called()
    finally:
        if worker.is_alive():
            runtime.control(record, "stop")
            worker.join(5)
        socket_path.unlink(missing_ok=True)
        assert not worker.is_alive()


def test_shared_deadline_prevents_second_compose_mutation_after_first_overruns(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    config = Config(tmp_path, [])
    compose = Compose(config)
    compose.recover_existing = True
    sync = Mock()
    sync.start.side_effect = lambda: clock.__setitem__(0, clock[0] + 3)
    monkeypatch.setattr(runtime, "Synchronizer", lambda *_a, **_kw: sync)
    model = {"name": "fixture", "services": {"app": {}}}
    observations = []

    def status(*_a, **kwargs):
        observations.append(kwargs)
        clock[0] += 2
        return [{"Service": "app", "Project": "fixture", "State": "exited", "ExitCode": 7}]

    mutations = []

    def mutate(command, **kwargs):
        mutations.append((command, kwargs))
        clock[0] += 6
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(runtime, "service_status", status)
    monkeypatch.setattr(runtime, "run", mutate)
    with pytest.raises(runtime.StartupIncomplete, match="deadline expired"):
        runtime.launch_stack(compose, model, {}, "012345abcdef", 10, deadline=110)
    assert len(mutations) == len(observations) == 1
    assert "--force-recreate" in mutations[0][0]
    assert mutations[0][1]["timeout"] == 5
    assert observations[0]["deadline"] == 110


def test_global_deadline_clips_every_phase_and_rejects_late_ready(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    compose = Compose(Config(tmp_path, []))
    compose.recover_existing = True
    sync = Mock()
    sync.start.side_effect = lambda: clock.__setitem__(0, clock[0] + 2)
    monkeypatch.setattr(runtime, "Synchronizer", lambda *_a, **_kw: sync)
    observations = []

    def status(*_a, **kwargs):
        observations.append(kwargs["deadline"])
        clock[0] += 2
        return [{"Service": "app", "State": "exited" if len(observations) == 1 else "running"}]

    timeouts = []

    def mutate(_command, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += 2 if len(timeouts) == 1 else 1
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(runtime, "service_status", status)
    monkeypatch.setattr(runtime, "run", mutate)
    with pytest.raises(runtime.StartupIncomplete, match="deadline expired"):
        runtime.launch_stack(compose, {"services": {"app": {}}}, {}, "012345abcdef", 60, deadline=108)
    assert timeouts == [4, 2]
    assert observations == [108, 108]


@pytest.mark.parametrize("cancel", [False, True])
def test_initial_sync_is_actively_cancelled_before_any_compose_mutation(monkeypatch, tmp_path, cancel):
    clock = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    entered, released, stopped = threading.Event(), threading.Event(), threading.Event()
    sync = Mock()

    def start():
        entered.set()
        assert released.wait(3), "Initial sync watchdog did not cancel the blocked copy"
        raise PodgroveError("fixture sync cancelled")

    sync.start.side_effect = start
    sync.cancel.side_effect = released.set
    monkeypatch.setattr(runtime, "Synchronizer", lambda *_a, **_kw: sync)
    run = Mock()
    monkeypatch.setattr(runtime, "run", run)
    failures = []

    def launch():
        try:
            runtime.launch_stack(Compose(Config(tmp_path, [])), {"services": {"app": {}}}, {},
                                 "012345abcdef", 10, cancel_event=stopped)
        except PodgroveError as exc:
            failures.append(str(exc))

    worker = threading.Thread(target=launch)
    worker.start()
    try:
        assert entered.wait(2)
        if cancel:
            stopped.set()
        else:
            clock[0] = 111
        worker.join(3)
        assert not worker.is_alive()
        assert failures == ["fixture sync cancelled"]
        sync.cancel.assert_called_once()
        sync.close.assert_called_once()
        run.assert_not_called()
    finally:
        released.set()
        worker.join(3)


def test_startup_status_retry_consumes_the_same_deadline(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    timeouts = []

    def read(_command, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        raise PodgroveError("connection reset by peer")

    monkeypatch.setattr(runtime, "run", read)
    with pytest.raises(PodgroveError, match="deadline expired"):
        runtime.service_status(Compose(Config(tmp_path, [])), {}, deadline=103)
    assert timeouts == [3]


@pytest.mark.parametrize("cancel_source", ["startup", "tunnel"])
def test_initial_tunnel_reads_keep_both_cancellation_sources(monkeypatch, cancel_source):
    clock = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    startup, tunnel = threading.Event(), threading.Event()
    captured = []

    def read(*_args, **kwargs):
        captured.append(kwargs)
        assert kwargs["timeout"] == 3
        assert not kwargs["cancel_event"].is_set()
        (startup if cancel_source == "startup" else tunnel).set()
        assert kwargs["cancel_event"].is_set()
        return SimpleNamespace(stdout="{}", returncode=0)

    proxy = runtime._StartupKube(SimpleNamespace(call=read), 103, startup)
    with pytest.raises(PodgroveError, match="cancelled"):
        proxy.call("get", "pod", "fixture", timeout=35, cancel_event=tunnel)
    assert len(captured) == 1


@pytest.mark.parametrize("deadline", [None, 150])
def test_shared_startup_deadline_preserves_individual_compose_phase_caps(monkeypatch, tmp_path, deadline):
    clock = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    compose = Compose(Config(tmp_path, []))
    compose.recover_existing = True
    sync = Mock()
    sync.start.side_effect = lambda: clock.__setitem__(0, clock[0] + 2)
    monkeypatch.setattr(runtime, "Synchronizer", lambda *_a, **_kw: sync)
    observations = []

    def status(*_a, **_kwargs):
        observations.append(1)
        return [{"Service": "app", "State": "exited" if len(observations) == 1 else "running"}]

    timeouts = []

    def mutate(_command, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += 8
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(runtime, "service_status", status)
    monkeypatch.setattr(runtime, "run", mutate)
    observed, elapsed, _ = runtime.launch_stack(compose, {"services": {"app": {}}}, {}, "012345abcdef",
                                                10, deadline=deadline)
    assert observed is sync and elapsed == 18
    assert timeouts == [10, 10]
