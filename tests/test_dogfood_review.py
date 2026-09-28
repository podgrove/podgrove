"""Independent CLI health/recovery regressions; no sockets or external tools."""
from copy import deepcopy
import hashlib
import json
import time
from unittest.mock import Mock

import pytest

from podgrove import cli, runtime, state
from podgrove.compose import Compose
from podgrove.errors import PodgroveError
from podgrove.session_status import observed


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "capture_anchor", lambda *_: {"fixture": "owned-controller-and-pvc"})
    root = tmp_path / "project"
    root.mkdir()
    config_file = root / "podgrove.yml"
    config_file.write_text("cluster: {context: review, namespace: team, storage_class: approved}\n")
    (root / "compose.yml").write_text("name: review-project\nservices:\n  api:\n    image: busybox:1.37\n")
    monkeypatch.chdir(root)
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    model = {"name": "review-project", "services": {"api": {"image": "busybox:1.37"}}}
    monkeypatch.setattr(Compose, "model", lambda _: deepcopy(model))
    monkeypatch.setattr(cli, "validate_port_plan", Mock())
    config = cli.load_config(root)
    fingerprint = hashlib.sha256(json.dumps({"model": model, "forward": config.forward,
        "ttl": config.ttl_seconds, "network": config.network}, sort_keys=True).encode()).hexdigest()
    data = {"identity": state.identity(root), "root": str(root), "context": "review", "namespace": "team",
            "namespace_mode": "shared", "node_mode": "shared", "status": "ready",
            "docker_host": "tcp://127.0.0.1:12345", "socket": str(tmp_path / "no-live-socket"),
            "compose_fingerprint": fingerprint, "compose_project": "review-project", "compose_services": ["api"],
            "ports": [{"service": "api", "target": 80, "local": 23456, "url": "http://127.0.0.1:23456"}],
            "forward_status": {"state": "ready", "checked_at": time.time()},
            "sync_status": {"state": "ready", "checked_at": time.time()}}
    path = state.state_path(root, "review")
    state.write(path, data)
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    monkeypatch.setattr(runtime, "is_running", lambda _: True)
    return root, path, data, kube


def invoke(*args):
    return cli.execute(cli.parser().parse_args(list(args)))


@pytest.mark.parametrize("health,expected", [("healthy", "degraded"), ("unhealthy", "unhealthy")])
def test_current_sync_retry_and_current_container_failure_survive_status_projection(recorded, monkeypatch, capsys, health, expected):
    _, path, saved, _ = recorded
    monkeypatch.setattr(runtime, "control", Mock(return_value={"ok": True, "status": "ready",
        "forward_status": {"state": "ready"}, "sync_status": {"state": "retrying"}}))
    monkeypatch.setattr(runtime, "service_status", lambda *_: [{"Service": "api", "State": "running", "Health": health}])
    assert invoke("status", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == expected and result["sync_status"]["state"] == "retrying"
    assert result["ports"][0]["status"] == "ready"  # Transport readiness remains distinct.
    assert state.read(path) == saved


def test_rejected_current_ping_cannot_reuse_saved_ready_endpoints():
    saved = {"status": "ready", "ports": [{"service": "api", "target": 80, "local": 1234}],
             "forward_status": {"state": "ready"}, "sync_status": {"state": "ready"}}
    rejected = {"ok": False, "status": "ready", "forward_status": {"state": "ready"},
                "sync_status": {"state": "ready"}}
    before = deepcopy(saved)
    result = observed(saved, connected=True, ping=rejected)
    assert result["status"] == "disconnected"
    assert result["ports"][0]["status"] == "disconnected"
    assert saved == before


def test_env_refuses_a_ping_rejected_after_initial_is_running_check(recorded, monkeypatch, capsys):
    monkeypatch.setattr(runtime, "control", lambda *_: {"ok": False})
    with pytest.raises(PodgroveError, match="not ready|disconnected"):
        invoke("env")
    assert capsys.readouterr().out == ""


def test_up_rejected_fresh_ping_refuses_rebuild_stop_or_success(recorded, monkeypatch, capsys):
    _, path, data, kube = recorded
    control = Mock(return_value={"ok": False, "status": "ready", "forward_status": {"state": "ready"}})
    monkeypatch.setattr(runtime, "control", control)
    spawn = Mock(side_effect=AssertionError("Rejected authentication cannot authorize another supervisor"))
    monkeypatch.setattr(runtime, "spawn", spawn)
    with pytest.raises(PodgroveError):
        invoke("up", "--json")
    assert capsys.readouterr().out == ""
    control.assert_called_once_with(data, "ping")
    kube.create_environment.assert_not_called()
    kube.destroy.assert_not_called()
    spawn.assert_not_called()
    assert state.read(path) == data


@pytest.mark.parametrize("forward,sync", [("unknown", "ready"), ("reconnecting", "ready"), ("ready", "retrying")])
def test_unchanged_up_does_not_report_success_until_transport_and_sync_are_ready(recorded, monkeypatch, capsys, forward, sync):
    _, path, data, kube = recorded
    control = Mock(return_value={"ok": True, "status": "ready", "forward_status": {"state": forward},
                                 "sync_status": {"state": sync}})
    monkeypatch.setattr(runtime, "control", control)
    spawn = Mock(side_effect=AssertionError("Recovering existing session must not start another engine"))
    monkeypatch.setattr(runtime, "spawn", spawn)
    assert invoke("up", "--json") == 1
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"
    control.assert_called_once_with(data, "ping")
    kube.reconcile_network_policy.assert_called_once()
    kube.create_environment.assert_not_called()
    kube.destroy.assert_not_called()
    assert state.read(path) == data


def test_up_after_exhausted_forward_recovery_reuses_owned_environment_names(recorded, monkeypatch, capsys):
    _, path, data, kube = recorded
    data.update(status="degraded", forward_status={"state": "disconnected", "attempts": 5})
    state.write(path, data)
    control = Mock(return_value={"ok": True, "status": "degraded", "forward_status": {"state": "disconnected"}})
    monkeypatch.setattr(runtime, "control", control)
    def spawn(new_path):
        assert new_path == path
        current = state.read(new_path)
        assert current["identity"] == data["identity"] and current["namespace"] == data["namespace"]
        current.update(status="ready", ports=data["ports"], docker_host="tcp://127.0.0.1:34567")
        state.write(new_path, current)
    monkeypatch.setattr(runtime, "spawn", spawn)
    assert invoke("up", "--json") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert [call.args[1] for call in control.call_args_list] == ["ping", "stop"]
    kube.destroy.assert_not_called()
    resources, identity = kube.create_environment.call_args.args
    assert identity == data["identity"]
    assert {(item["kind"], item["metadata"]["name"]) for item in resources} == {
        (kind, "pg-" + identity) for kind in ("NetworkPolicy", "PersistentVolumeClaim", "ConfigMap", "Service", "PodDisruptionBudget", "StatefulSet")}
    assert all(item["metadata"]["namespace"] == data["namespace"] for item in resources)


@pytest.mark.parametrize("sync_config", ["", "sync: {exclude: []}\n"])
def test_new_empty_sync_config_does_not_invalidate_preexisting_fingerprint(recorded, monkeypatch, capsys, sync_config):
    root, path, saved, kube = recorded
    config_file = root / "podgrove.yml"
    config_file.write_text(config_file.read_text() + sync_config)
    monkeypatch.setattr(runtime, "control", Mock(return_value={"ok": True, "status": "ready",
                                                              "forward_status": {"state": "ready"}}))
    monkeypatch.setattr(runtime, "spawn", Mock(side_effect=AssertionError("Unchanged model must not restart")))
    assert invoke("up", "--json") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    kube.create_environment.assert_not_called()
    assert state.read(path) == saved


def test_retained_logs_tunnel_start_failure_closes_only_temporary_transport(recorded, monkeypatch):
    from podgrove import docker_tunnel
    _, path, data, kube = recorded
    tunnel = Mock()
    tunnel.start.side_effect = PodgroveError("owned engine is unavailable")
    monkeypatch.setattr(docker_tunnel, "DockerTunnel", Mock(return_value=tunnel))
    process = Mock(side_effect=AssertionError("No log process after transport failure"))
    monkeypatch.setattr(cli.subprocess, "call", process)
    args = cli.parser().parse_args(["logs", "api"])
    with pytest.raises(PodgroveError, match="owned engine is unavailable"):
        cli.retained_logs(data, kube, args)
    tunnel.close.assert_called_once()
    kube.destroy.assert_not_called()
    assert state.read(path) == data
