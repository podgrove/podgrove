"""CLI recovery, protection and connectivity integration without external APIs."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from podgrove import cli, runtime, state
from podgrove.compose import Compose
from podgrove.config import load_config
from podgrove.errors import PodgroveError
from podgrove.fingerprint import launch_fingerprint


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = (tmp_path / "application").resolve()
    root.mkdir()
    (root / "compose.yml").write_text("services: {app: {image: busybox:1.37}}\n")
    settings = {"cluster": {"context": "offline-findings", "namespace": "approved-team", "storage_class": "approved"}}
    (root / "podgrove.yml").write_text(yaml.safe_dump(settings))
    monkeypatch.chdir(root)
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("PODGROVE_CONTEXT", raising=False)
    secret = "fixture-not-for-output-4bd912"
    model = {"name": "findings-app", "services": {"app": {"image": "busybox:1.37", "environment": {"PRIVATE_VALUE": secret}}}}
    monkeypatch.setattr(Compose, "model", lambda _: deepcopy(model))
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    running = Mock(return_value=False)
    monkeypatch.setattr(runtime, "is_running", running)
    ping = {"ok": True, "status": "ready", "forward_status": {"state": "disabled"}, "sync_status": {"state": "ready"}}
    control = Mock(side_effect=lambda data, operation: deepcopy(ping) if operation == "ping" else {"ok": True})
    monkeypatch.setattr(runtime, "control", control)
    ident = state.identity(root)
    path = state.state_path(root, "offline-findings")
    events = []
    kube.create_environment.side_effect = lambda *_: events.append("create")
    anchor = {"identity": ident, "context": "offline-findings", "namespace": "approved-team",
              "statefulset_uid": "original-controller", "pvc_uid": "original-pvc", "controller_spec_sha256": "a" * 64}
    def capture(candidate, identity):
        assert events[-1] == "create" and candidate is kube and identity == ident
        events.append("anchor")
        return dict(anchor)
    monkeypatch.setattr(cli, "capture_anchor", capture)
    def spawned(selected):
        assert selected == path
        data = state.read(path)
        assert data["startup_anchor"] == anchor
        events.append("spawn")
        data.update(status="ready", forward_status={"state": "disabled"}, sync_status={"state": "ready"})
        state.write(path, data)
    spawn = Mock(side_effect=spawned)
    monkeypatch.setattr(runtime, "spawn", spawn)
    def record(**extra):
        config = load_config(root)
        fingerprint = launch_fingerprint(model, config)
        saved = {"identity": ident, "root": str(root), "context": "offline-findings", "namespace": "approved-team",
                 "namespace_mode": "shared", "node_mode": "shared", "placement": config.placement,
                 "status": "ready", "docker_host": "tcp://127.0.0.1:1", "socket": str(tmp_path / "not-running.sock"),
                 "compose_project": model["name"], "compose_services": ["app"], "ports": [],
                 "compose_fingerprint": fingerprint.digest, "compose_fingerprint_format": "compose-v1",
                 "forward_status": {"state": "disabled"}, "sync_status": {"state": "ready"}, **extra}
        state.write(path, saved)
        running.return_value = True
        return saved
    return SimpleNamespace(root=root, path=path, settings=settings, model=model, kube=kube, running=running,
                           ping=ping, control=control, spawn=spawn, anchor=anchor, events=events,
                           secret=secret, record=record)


def invoke(command="up", *options):
    return cli.execute(cli.parser().parse_args([command, "--json", *options]))


@pytest.mark.parametrize("bind", [False, True])
def test_healthy_up_remirrors_bind_sources_but_preserves_no_bind_fast_path(project, capsys, bind):
    source = project.root / "source.txt"
    source.write_text("before")
    if bind:
        project.model["services"]["app"]["volumes"] = [{"type": "bind", "source": str(source), "target": "/app/source.txt"}]
    project.record()
    source.write_text("after")
    assert invoke() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    if bind:
        assert [call.args[1] for call in project.control.call_args_list] == ["ping", "stop"]
        assert project.events == ["create", "anchor", "spawn"]
        project.kube.create_environment.assert_called_once()
    else:
        assert [call.args[1] for call in project.control.call_args_list] == ["ping"]
        project.spawn.assert_not_called()
        project.kube.create_environment.assert_not_called()
    project.kube.destroy.assert_not_called()
    project.kube.reconcile_engine_protection.assert_called_once()


@pytest.mark.parametrize("status,startup", [("unhealthy", None), ("degraded", {"state": "failed"})])
def test_unhealthy_or_failed_startup_up_explicitly_retries_without_deleting_engine(project, capsys, status, startup):
    project.record(status=status, **({"startup_status": startup} if startup else {}))
    project.ping["status"] = status
    if startup:
        project.ping["startup_status"] = startup
    assert invoke() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert [call.args[1] for call in project.control.call_args_list] == ["ping", "stop"]
    assert project.events == ["create", "anchor", "spawn"]
    project.kube.destroy.assert_not_called()


def test_startup_anchor_and_project_are_persisted_before_spawn_without_model_secrets(project, capsys):
    assert invoke() == 0
    output = capsys.readouterr().out
    stored = project.path.read_text()
    assert project.secret not in output + stored
    saved = state.read(project.path)
    assert saved["startup_anchor"] == project.anchor
    assert saved["compose_project"] == "findings-app"
    assert project.events == ["create", "anchor", "spawn"]
    resources, identity = project.kube.create_environment.call_args.args
    lease = next(item for item in resources if item["kind"] == "ConfigMap")
    assert lease["data"]["compose_project"] == "findings-app"
    assert identity == saved["identity"]
    assert project.secret not in json.dumps(resources)


def test_anchor_failure_retains_diagnostic_state_without_spawning_or_deleting(project, monkeypatch):
    def replaced(*_):
        raise PodgroveError("Original PVC identity changed")
    monkeypatch.setattr(cli, "capture_anchor", replaced)
    with pytest.raises(PodgroveError, match="Original PVC identity changed"):
        invoke()
    saved = state.read(project.path)
    assert saved["status"] == "error" and saved["error"] == "Original PVC identity changed"
    project.spawn.assert_not_called()
    project.kube.destroy.assert_not_called()


@pytest.mark.parametrize("command", ["doctor", "up"])
def test_native_placement_reaches_doctor_and_up_without_node_inventory(project, capsys, command):
    placement = {"nodeSelector": {"example.com/capacity-type": "on-demand"}}
    (project.root / "podgrove.yml").write_text(yaml.safe_dump({**project.settings, "placement": placement}))
    assert invoke(command) == 0
    capsys.readouterr()
    resources = (project.kube.check_admission if command == "doctor" else project.kube.create_environment).call_args.args[0]
    controller = next(item for item in resources if item["kind"] == "StatefulSet")
    assert controller["spec"]["template"]["spec"]["nodeSelector"] == {
        "kubernetes.io/os": "linux", "example.com/capacity-type": "on-demand"}
    project.kube.get.assert_not_called()
    if command == "up":
        assert state.read(project.path)["placement"] == placement


def test_partial_application_failure_returns_nonzero_retains_state_and_endpoints(project, capsys):
    def failed_start(path):
        saved = state.read(path)
        saved.update(status="unhealthy", startup_status={"state": "failed", "error": "one service failed"},
                     ports=[{"service": "app", "target": 80, "local": 32123, "status": "ready"}],
                     forward_status={"state": "ready"}, sync_status={"state": "ready"})
        state.write(path, saved)
    project.spawn.side_effect = failed_start
    assert invoke() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "unhealthy" and result["startup_status"]["state"] == "failed"
    assert result["ports"][0]["local"] == 32123
    assert project.path.exists() and state.read(project.path)["startup_anchor"] == project.anchor
    project.kube.destroy.assert_not_called()
    assert project.control.call_count == 0


@pytest.mark.parametrize("setting", [
    {"reverse": [{"local_port": 8080}]},
    {"network": {"pod_to_pod": "open"}},
])
def test_changed_connectivity_configuration_invalidates_fast_path_and_is_saved(project, capsys, setting):
    original = project.record()
    (project.root / "podgrove.yml").write_text(yaml.safe_dump({**project.settings, **setting}))
    assert invoke() == 0
    capsys.readouterr()
    saved = state.read(project.path)
    assert saved["compose_fingerprint"] != original["compose_fingerprint"]
    assert [call.args[1] for call in project.control.call_args_list] == ["ping", "stop"]
    for key in setting:
        assert saved[key]
    project.kube.destroy.assert_not_called()


def test_doctor_refuses_missing_engine_safeguards_without_repairing_them(project):
    project.kube.check_engine_protection.side_effect = PodgroveError("Existing engine eviction protection is missing")
    with pytest.raises(PodgroveError, match="eviction protection is missing"):
        invoke("doctor")
    project.kube.check_admission.assert_called_once()
    project.kube.check_engine_protection.assert_called_once()
    project.kube.reconcile_engine_protection.assert_not_called()
    project.kube.create_environment.assert_not_called()
    assert not project.path.exists()


def test_existing_legacy_connect_grants_refuse_before_mutation(project):
    original = project.record()
    original["connect"] = [{"name": "db", "environment": "abcdef123456", "service": "db", "port": 27017}]
    state.write(project.path, original)
    with pytest.raises(PodgroveError, match="previous Podgrove version"):
        invoke()
    project.kube.create_environment.assert_not_called()
    project.kube.reconcile_network_policy.assert_not_called()
    project.control.assert_not_called()


@pytest.mark.parametrize("before,after", [("selected", "open"), ("open", "disabled"), ("disabled", "selected")])
def test_network_change_stops_previous_monitor_before_applying_new_rules(project, capsys, before, after):
    (project.root / "podgrove.yml").write_text(yaml.safe_dump({**project.settings, "network": {"pod_to_pod": before}}))
    project.record(network=load_config(project.root).network)
    (project.root / "podgrove.yml").write_text(yaml.safe_dump({**project.settings, "network": {"pod_to_pod": after}}))
    project.kube.create_environment.side_effect = lambda *_: project.events.append("create") if project.control.call_args.args[1] == "stop" else pytest.fail("policy written before monitor stopped")
    assert invoke() == 0
    capsys.readouterr()
    project.kube.reconcile_network_policy.assert_not_called()
    assert project.events == ["create", "anchor", "spawn"]


def test_idempotent_selected_up_does_not_erase_resolved_peer_rules(project, capsys):
    (project.root / "podgrove.yml").write_text(yaml.safe_dump({**project.settings, "network": {"pod_to_pod": "selected"}}))
    project.record(network=load_config(project.root).network)
    assert invoke() == 0
    capsys.readouterr()
    project.kube.reconcile_network_policy.assert_not_called()
    project.kube.create_environment.assert_not_called()
    assert [call.args[1] for call in project.control.call_args_list] == ["ping"]


def test_completed_startup_with_unavailable_peer_discovery_returns_diagnostic(project, capsys):
    def unavailable(path):
        saved = state.read(path)
        saved.update(status="degraded", startup_status={"state": "ready"},
                     pod_network_status={"state": "unavailable", "endpoints": [], "error": "Peer namespace read forbidden"})
        state.write(path, saved)
    project.spawn.side_effect = unavailable
    assert invoke() == 1
    output = json.loads(capsys.readouterr().out)
    assert output["pod_network_status"]["error"] == "Peer namespace read forbidden"
    assert state.read(project.path)["status"] == "degraded"
    project.kube.destroy.assert_not_called()
