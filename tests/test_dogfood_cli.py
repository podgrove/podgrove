"""Regressions for the local workflow reported in the reported development workflows."""
import json
import hashlib
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from podgrove import cli, runtime, state
from podgrove.compose import Compose
from podgrove.errors import PodgroveError
from podgrove.session_status import observed


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "capture_anchor", lambda *_: {"fixture": "owned-controller-and-pvc"})
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "podgrove.yml").write_text("cluster: {context: test, namespace: approved}\n")
    (root / "compose.yaml").write_text("name: test-project\nservices:\n  api:\n    image: busybox:1.37\n")
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(root)
    data = {"identity": state.identity(root), "root": str(root), "context": "test", "namespace": "approved",
            "namespace_mode": "shared", "node_mode": "shared", "status": "ready", "docker_host": "tcp://127.0.0.1:12345",
            "ports": [{"service": "api", "target": 80, "local": 23456, "url": "http://127.0.0.1:23456"}],
            "forward_status": {"state": "ready", "checked_at": time.time()},
            "compose_project": "test-project", "compose_services": ["api"]}
    path = state.state_path(root, "test")
    state.write(path, data)
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    return root, path, data, kube


@pytest.mark.parametrize("forward", ["ready", "reconnecting", "disconnected"])
def test_healthy_containers_cannot_mask_dead_forward(session, monkeypatch, capsys, forward):
    _, _, data, _ = session
    monkeypatch.setattr(runtime, "is_running", lambda _: True)
    control = Mock(return_value={"ok": True, "forward_status": {"state": forward, "checked_at": time.time()}})
    monkeypatch.setattr(runtime, "control", control)
    monkeypatch.setattr(Compose, "model", lambda _: {"services": {"api": {}}})
    monkeypatch.setattr(runtime, "service_status", lambda *_: [{"Service": "api", "State": "running", "Health": "healthy"}])
    assert cli.execute(cli.parser().parse_args(["status", "--json"])) == (0 if forward == "ready" else 1)
    result = json.loads(capsys.readouterr().out)
    assert result["ports"][0]["status"] == forward
    assert result["status"] == ("ready" if forward == "ready" else "degraded")
    assert result["identity"] == data["identity"] and result["root"] == data["root"]


def test_status_eof_reports_stale_health_as_json_and_next_command_still_works(session, monkeypatch, capsys):
    _, path, before, _ = session
    monkeypatch.setattr(runtime, "is_running", lambda _: True)
    monkeypatch.setattr(runtime, "control", lambda *_: {"ok": True, "status": "ready",
                                                      "forward_status": {"state": "ready", "checked_at": time.time()}})
    monkeypatch.setattr(Compose, "model", lambda _: {"services": {"api": {}}})
    status = Mock(side_effect=[runtime.TransientDockerReadError("temporary EOF"),
                              [{"Service": "api", "State": "running", "Health": "healthy"}]])
    monkeypatch.setattr(runtime, "service_status", status)
    stop = Mock(side_effect=AssertionError("A status read must not stop the session"))
    monkeypatch.setattr(runtime, "stop_session", stop)
    args = cli.parser().parse_args(["status", "--json"])
    assert cli.execute(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "degraded" and result["health_status"]["state"] == "unavailable"
    assert result["ports"][0]["status"] == "ready"
    assert state.read(path) == before
    assert cli.execute(cli.parser().parse_args(["status", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    stop.assert_not_called()


@pytest.mark.parametrize("command", [["status", "--json"], ["status", "--all", "--json"]])
def test_disconnected_status_marks_planned_endpoints(session, monkeypatch, capsys, command):
    monkeypatch.setattr(runtime, "is_running", lambda _: False)
    cli.execute(cli.parser().parse_args(command))
    result = json.loads(capsys.readouterr().out)
    if "environments" in result:
        result = result["environments"][0]
    assert result["status"] == "disconnected"
    assert result["ports"][0]["status"] == "disconnected"


@pytest.mark.parametrize("forward", ["ready", "reconnecting", "disconnected", "unknown"])
def test_env_exports_only_ready_addresses(session, monkeypatch, capsys, forward):
    monkeypatch.setattr(runtime, "is_running", lambda _: True)
    monkeypatch.setattr(runtime, "control", lambda *_: {"ok": True, "forward_status": {"state": forward}})
    monkeypatch.setattr(cli, "Compose", Mock(side_effect=AssertionError("env does not parse Compose")))
    if forward != "ready":
        with pytest.raises(PodgroveError, match="not ready"):
            cli.execute(cli.parser().parse_args(["env"]))
        assert capsys.readouterr().out == ""
    else:
        assert cli.execute(cli.parser().parse_args(["env", "--json"])) == 0
        assert json.loads(capsys.readouterr().out) == {"PODGROVE_API_80_HOST": "127.0.0.1",
            "PODGROVE_API_80_PORT": "23456", "PODGROVE_API_80_URL": "http://127.0.0.1:23456"}


def test_endpoint_name_collisions_are_explicit():
    with pytest.raises(PodgroveError, match="collide"):
        cli.endpoint_environment([{"service": name, "target": 80, "local": 3000} for name in ("a-b", "a_b")])


@pytest.mark.parametrize("changes", [{"target": "80; false"}, {"local": "$(false)"}, {"target": True},
                                    {"service": "api\nfalse"}, {"local": 0}, {"target": 65536}])
def test_endpoint_exports_reject_corrupt_shell_fields(changes):
    with pytest.raises(PodgroveError, match="metadata is invalid"):
        cli.endpoint_environment([{"service": "api", "target": 80, "local": 3000, **changes}])


@pytest.mark.parametrize("fail", [False, True])
def test_retained_logs_after_failure_do_not_require_original_config_or_restart(session, monkeypatch, fail):
    from podgrove import docker_tunnel
    root, path, data, kube = session
    data.update(status="error", error="Build failed")
    data.pop("docker_host")
    state.write(path, data)
    (root / "compose.yaml").unlink()
    monkeypatch.setattr(runtime, "is_running", lambda _: False)
    monkeypatch.setattr(runtime, "spawn", Mock(side_effect=AssertionError("No restart for diagnostics")))
    monkeypatch.setattr(cli, "load_config", Mock(side_effect=AssertionError("No original Compose files")))
    tunnel = Mock(port=24567)
    monkeypatch.setattr(docker_tunnel, "DockerTunnel", Mock(return_value=tunnel))
    models = []
    def logs(command, env, cwd):
        assert command[:6] == ["docker", "compose", "--ansi", "never", "--project-name", "test-project"]
        temporary = Path(command[command.index("--file") + 1])
        models.append(temporary)
        assert json.loads(temporary.read_text()) == {"services": {"api": {"image": "scratch"}}}
        assert command[-5:] == ["logs", "--tail", "20", "--follow", "api"]
        assert "up" not in command and "--follow" in command
        assert env["DOCKER_HOST"] == "tcp://127.0.0.1:24567"
        if fail:
            raise KeyboardInterrupt()
        return 0
    monkeypatch.setattr(cli.subprocess, "call", logs)
    args = cli.parser().parse_args(["logs", "api", "--tail", "20", "--follow"])
    if fail:
        with pytest.raises(KeyboardInterrupt):
            cli.execute(args)
    else:
        assert cli.execute(args) == 0
    tunnel.start.assert_called_once()
    tunnel.close.assert_called_once()
    kube.create_environment.assert_not_called()
    assert state.read(path) == data and all(not model.exists() for model in models)


@pytest.mark.parametrize("scope,expected", [([], "current"), (["--all"], None), (["--environment", "abcdef012345"], "abcdef012345")])
def test_reap_defaults_to_current_worktree(session, monkeypatch, scope, expected):
    _, _, data, kube = session
    reap = Mock(return_value=[])
    monkeypatch.setattr(cli, "reap", reap)
    assert cli.execute(cli.parser().parse_args(["reap", "--dry-run", *scope])) == 0
    reap.assert_called_once_with(kube, True, identity=data["identity"] if expected == "current" else expected)


def test_stale_snapshot_is_not_endpoint_health(session):
    _, _, data, _ = session
    data["forward_status"]["checked_at"] = time.time() - 60
    assert observed(data)["ports"][0]["status"] == "unknown"
    assert observed(data)["status"] == "degraded"


def test_cold_first_and_second_up_use_same_actual_compose_model(session, monkeypatch, capsys):
    root, path, _, kube = session
    path.unlink()
    started = []
    def spawn(path):
        started.append(path)
        data = state.read(path)
        data.update(status="ready", docker_host="tcp://127.0.0.1:12345", ports=[])
        state.write(path, data)
    monkeypatch.setattr(runtime, "spawn", spawn)
    monkeypatch.setattr(runtime, "is_running", lambda _: bool(started))
    for _ in range(3):
        assert cli.execute(cli.parser().parse_args(["up", "--json"])) == 0
    outputs = capsys.readouterr().out
    assert outputs.count('"status": "ready"') == 3
    assert len(started) == kube.create_environment.call_count == 1
    data = state.read(path)
    assert data["config_path"] == str(root / "podgrove.yml")
    assert data["files"] == [str(root / "compose.yaml")]
    assert data["compose_project"] == "test-project"
    config = cli.load_config(root)
    original = {"model": Compose(config).model(), "forward": config.forward,
                "ttl": config.ttl_seconds, "network": config.network}
    assert data["compose_fingerprint"] == hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
