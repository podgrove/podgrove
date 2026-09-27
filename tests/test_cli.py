import json
import socket
import time
from unittest.mock import Mock

import pytest

from podgrove import cli, runtime, state
from podgrove.compose import Compose
from podgrove.config import Config, default_tainted_nodes
from podgrove.errors import PodgroveError
from podgrove.kube import engine_pod_manifest


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "compose.yaml").write_text("services:\n  app:\n    image: busybox:1.37\n")
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    return root


def up_args(root, *extra):
    return cli.parser().parse_args(["up", "--context", "test-context", "--namespace", "podgrove-testing",
                                    "--project-directory", str(root), *extra])


def test_unknown_configuration_rejected_before_kubernetes_client(project, monkeypatch):
    (project / "podgrove.yml").write_text("typo: true\n")
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", kube)
    with pytest.raises(PodgroveError, match="typo"):
        cli.up(up_args(project), project)
    kube.assert_not_called()


def test_unsupported_compose_rejected_before_kubernetes_client(project, monkeypatch):
    monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {"network_mode": "host"}}})
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", kube)
    with pytest.raises(PodgroveError, match="network_mode"):
        cli.up(up_args(project), project)
    kube.assert_not_called()


def test_up_is_idempotent_with_own_explicit_local_forward(project, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        local_port = listener.getsockname()[1]
        config = Config(project, [project / "compose.yaml"],
                        forward=[{"service": "app", "port": 80, "local": local_port}])
        monkeypatch.setattr(cli, "load_config", lambda *_: config)
        monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {
            "ports": [{"target": 80, "published": "8080"}]
        }}})
        monkeypatch.setattr(runtime, "is_running", lambda *_: True)
        path = state.state_path(project, "test-context")
        state.write(path, {"namespace": "podgrove-testing", "status": "ready", "root": str(project),
                           "context": "test-context", "identity": state.identity(project), "node_mode": "shared"})
        kube = Mock()
        monkeypatch.setattr(cli, "Kube", kube)
        assert cli.up(up_args(project), project) == 0
        kube.return_value.create_environment.assert_not_called()
        kube.return_value.reconcile_network_policy.assert_called_once()
        rendered, ident = kube.return_value.reconcile_network_policy.call_args.args
        assert ident == state.identity(project)
        assert any(item["kind"] == "NetworkPolicy" for item in rendered)


def test_running_up_does_not_report_success_if_policy_repair_is_refused(project, monkeypatch):
    monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {"image": "busybox:1.37"}}})
    monkeypatch.setattr(runtime, "is_running", lambda *_: True)
    path = state.state_path(project, "test-context")
    original = {"namespace": "podgrove-testing", "status": "ready", "root": str(project),
                "context": "test-context", "identity": state.identity(project), "node_mode": "shared"}
    state.write(path, original)
    kube = Mock()
    kube.reconcile_network_policy.side_effect = PodgroveError("NetworkPolicy ownership changed")
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    stop = Mock()
    monkeypatch.setattr(runtime, "control", stop)
    with pytest.raises(PodgroveError, match="NetworkPolicy ownership changed"):
        cli.up(up_args(project), project)
    assert state.read(path) == original
    kube.create_environment.assert_not_called()
    stop.assert_not_called()


def test_changed_network_settings_refresh_recorded_configuration_with_same_compose(project, monkeypatch):
    monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {"image": "busybox:1.37"}}})
    kube, stop = Mock(), Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    monkeypatch.setattr(runtime, "control", stop)
    def ready(path):
        data = state.read(path)
        data["status"] = "ready"
        state.write(path, data)
    monkeypatch.setattr(runtime, "spawn", ready)
    (project / "podgrove.yml").write_text('network: {blocked_cidrs: ["44.55.0.0/16"]}\n')
    assert cli.up(up_args(project), project) == 0
    path = state.state_path(project, "test-context")
    old = state.read(path)
    monkeypatch.setattr(runtime, "is_running", lambda *_: True)
    (project / "podgrove.yml").write_text('network: {blocked_cidrs: ["44.55.0.0/16", "45.56.0.0/16"]}\n')
    assert cli.up(up_args(project), project) == 0
    current = state.read(path)
    assert current["compose_fingerprint"] != old["compose_fingerprint"]
    assert current["network"]["blocked_cidrs"] == ["44.55.0.0/16", "45.56.0.0/16"]
    assert current["namespace"] == old["namespace"]
    stop.assert_called_once_with(old, "stop")
    kube.reconcile_network_policy.assert_called_once()
    assert kube.create_environment.call_count == 2
    kube.destroy.assert_not_called()


def test_relative_custom_config_is_recorded_against_project_root(project, monkeypatch):
    (project / "custom.yml").write_text("version: 1\n")
    monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {"image": "busybox:1.37"}}})
    monkeypatch.setattr(cli, "Kube", Mock())
    def start(path):
        data = state.read(path)
        data["status"] = "ready"
        state.write(path, data)
    monkeypatch.setattr(runtime, "spawn", start)
    assert cli.up(up_args(project, "--config", "custom.yml"), project) == 0
    data = state.read(state.state_path(project, "test-context"))
    assert data["config_path"] == str(project / "custom.yml")


def test_refresh_stops_old_session_before_reusing_engine(project, monkeypatch):
    events = []
    monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {"image": "busybox:1.37"}}})
    path = state.state_path(project, "test-context")
    old_socket = project / "old.sock"
    old_socket.touch()
    state.write(path, {"identity": state.identity(project), "root": str(project), "context": "test-context",
                       "namespace": "podgrove-testing", "status": "ready", "socket": str(old_socket),
                       "node_mode": "shared",
                       "mr_url": "https://gitlab.com/group/project/-/merge_requests/123"})
    monkeypatch.setattr(runtime, "is_running", lambda *_: True)
    def control(*args):
        events.append("stop")
        old_socket.unlink()
    monkeypatch.setattr(runtime, "control", control)
    kube = Mock()
    kube.create_environment.side_effect = lambda *_: events.append("reuse")
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    def spawn(path):
        events.append("start")
        data = state.read(path)
        data["status"] = "ready"
        state.write(path, data)
    monkeypatch.setattr(runtime, "spawn", spawn)
    assert cli.up(up_args(project, "--refresh"), project) == 0
    assert events == ["stop", "reuse", "start"]
    kube.destroy.assert_not_called()
    assert state.read(path)["mr_url"] == "https://gitlab.com/group/project/-/merge_requests/123"


@pytest.mark.parametrize("changed", ["identity", "root", "context"])
def test_down_refuses_state_copied_from_different_worktree_or_context(project, monkeypatch, changed):
    data = {"namespace": "podgrove-testing", "status": "disconnected", "identity": state.identity(project),
            "root": str(project), "context": "test-context"}
    data[changed] = "abcdef123456" if changed == "identity" else (str(project.parent) if changed == "root" else "other-context")
    path = state.state_path(project, "test-context")
    state.write(path, data)
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", kube)
    args = cli.parser().parse_args(["down", "--context", "test-context", "--namespace", "podgrove-testing",
                                    "--project-directory", str(project)])
    with pytest.raises(PodgroveError, match="different worktree or cluster"):
        cli.execute(args)
    kube.return_value.destroy.assert_not_called()
    assert path.exists()


@pytest.mark.parametrize("text", ["[]", "null", "17", "not-json"])
def test_malformed_state_fails_closed(project, text):
    path = state.state_path(project, "test-context")
    path.write_text(text)
    with pytest.raises(PodgroveError, match="state"):
        state.read(path)


def test_interrupted_startup_cannot_spawn_competing_supervisor(project, monkeypatch):
    monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {"image": "busybox:1.37"}}})
    path = state.state_path(project, "test-context")
    previous = {"namespace": "podgrove-testing", "status": "starting", "identity": state.identity(project),
                "root": str(project), "context": "test-context", "created_at": time.time(), "token": "first-startup",
                "node_mode": "shared"}
    state.write(path, previous)
    spawn = Mock()
    monkeypatch.setattr(runtime, "spawn", spawn)
    monkeypatch.setattr(runtime, "is_running", lambda *_: False)
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", kube)
    with pytest.raises(PodgroveError, match="still starting"):
        cli.up(up_args(project), project)
    assert state.read(path) == previous
    kube.return_value.create_environment.assert_not_called()
    spawn.assert_not_called()


def lifecycle_args(root, command, *extra):
    return cli.parser().parse_args([command, "--context", "test-context", "--namespace", "podgrove-testing",
                                    "--project-directory", str(root), *extra])


@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("command", ["status", "logs", "exec"])
def test_starting_commands_report_startup_before_compose_or_docker(project, monkeypatch, capsys, connected, command):
    path = state.state_path(project, "test-context")
    original = {"namespace": "podgrove-testing", "status": "starting", "identity": state.identity(project),
                "root": str(project), "context": "test-context", "created_at": time.time()}
    state.write(path, original)
    monkeypatch.setattr(runtime, "is_running", lambda _: connected)
    control, config, compose, docker = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(runtime, "control", control)
    monkeypatch.setattr(cli, "load_config", config)
    monkeypatch.setattr(cli, "Compose", compose)
    monkeypatch.setattr(cli, "docker_environment", docker)
    monkeypatch.setattr(cli, "Kube", Mock())
    flags = ["--json"] if command == "status" else ["app", *(["--", "true"] if command == "exec" else [])]
    if command == "status":
        assert cli.execute(lifecycle_args(project, command, *flags)) == 1
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "starting"
        assert result["session_log"] == str(path.with_suffix(".log"))
    else:
        with pytest.raises(PodgroveError, match="still starting.*session log") as error:
            cli.execute(lifecycle_args(project, command, *flags))
        assert str(path.with_suffix(".log")) in str(error.value)
    assert state.read(path) == original
    for mock in (control, config, compose, docker):
        mock.assert_not_called()


@pytest.mark.parametrize("command", ["status", "logs", "exec"])
def test_connected_startup_error_without_docker_host_is_reported_without_keyerror(project, monkeypatch, capsys, command):
    path = state.state_path(project, "test-context")
    original = {"namespace": "podgrove-testing", "status": "error", "identity": state.identity(project),
                "root": str(project), "context": "test-context", "error": "Image build failed"}
    state.write(path, original)
    monkeypatch.setattr(runtime, "is_running", lambda _: True)
    monkeypatch.setattr(runtime, "control", Mock(side_effect=AssertionError("No control action before endpoint exists")))
    monkeypatch.setattr(cli, "Compose", Mock(side_effect=AssertionError("No Compose access before endpoint exists")))
    monkeypatch.setattr(cli, "Kube", Mock())
    flags = ["--json"] if command == "status" else ["app", *(["--", "true"] if command == "exec" else [])]
    if command == "status":
        assert cli.execute(lifecycle_args(project, command, *flags)) == 1
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "error" and result["error"] == "Image build failed"
    else:
        with pytest.raises(PodgroveError, match="Docker endpoint is not available.*session log"):
            cli.execute(lifecycle_args(project, command, *flags))


def test_ready_status_still_checks_compose_health(project, monkeypatch, capsys):
    path = state.state_path(project, "test-context")
    state.write(path, {"namespace": "podgrove-testing", "status": "ready", "identity": state.identity(project),
                       "root": str(project), "context": "test-context", "docker_host": "tcp://127.0.0.1:12345"})
    monkeypatch.setattr(runtime, "is_running", lambda _: True)
    control = Mock(return_value={"ok": True})
    monkeypatch.setattr(runtime, "control", control)
    monkeypatch.setattr(cli, "Kube", Mock())
    monkeypatch.setattr(Compose, "model", lambda _: {"services": {"app": {}}})
    status = Mock(return_value=[{"Service": "app", "State": "running", "Health": "healthy"}])
    monkeypatch.setattr(runtime, "service_status", status)
    assert cli.execute(lifecycle_args(project, "status", "--json")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready" and result["problems"] == []
    assert result["services"][0]["Service"] == "app"
    assert [call.args[1] for call in control.call_args_list] == ["ping", "touch"]
    assert status.call_args.args[1]["DOCKER_HOST"] == "tcp://127.0.0.1:12345"


def doctor_args(root, *extra):
    return cli.parser().parse_args(["doctor", "--context", "test-context", "--namespace", "podgrove-testing",
                                    "--project-directory", str(root), *extra])


def test_doctor_defaults_shared_without_any_project_files(tmp_path, monkeypatch):
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    compose = Mock()
    monkeypatch.setattr(cli, "Compose", compose)
    assert cli.execute(doctor_args(tmp_path)) == 0
    kube.preflight.assert_called_once_with(node_mode="shared", tainted_nodes=default_tainted_nodes())
    compose.assert_not_called()


def test_doctor_checks_admission_without_writes_or_compose(tmp_path, monkeypatch):
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    kube.check_admission.side_effect = PodgroveError("privileged engine denied")
    with pytest.raises(PodgroveError, match="privileged engine denied"):
        cli.execute(doctor_args(tmp_path))
    resources = kube.check_admission.call_args.args[0]
    controller = next(r for r in resources if r["kind"] == "StatefulSet")
    pod = engine_pod_manifest(controller)
    assert pod["metadata"]["namespace"] == "podgrove-testing"
    assert pod["metadata"]["labels"]["podgrove.dev/node-mode"] == "shared"
    kube.ensure_namespace.assert_called_once_with(state.identity(tmp_path))
    kube.create_environment.assert_not_called()


@pytest.mark.parametrize("namespace", ["podgrove-testing", "default", "custom-team"])
def test_doctor_requires_namespaced_bootstrap_marker_without_namespace_access(tmp_path, monkeypatch, capsys, namespace):
    from podgrove.kube import Kube
    kube = Kube("test-context", namespace, namespace_mode="shared")
    kube.get = Mock(return_value={})
    kube.preflight = Mock()
    kube.check_admission = Mock()
    kube.call = Mock(side_effect=AssertionError("Missing marker cannot trigger mutations"))
    factory = Mock(return_value=kube)
    monkeypatch.setattr(cli, "Kube", factory)
    args = cli.parser().parse_args(["doctor", "--context", "test-context", "--namespace", namespace,
                                    "--project-directory", str(tmp_path)])
    with pytest.raises(PodgroveError, match="Regenerate the namespaced bootstrap"):
        cli.execute(args)
    factory.assert_called_once_with("test-context", namespace, namespace_mode="shared")
    kube.get.assert_called_once_with("configmap", "podgrove-bootstrap")
    kube.preflight.assert_called_once_with(node_mode="shared", tainted_nodes=default_tainted_nodes())
    assert "after namespace creation" not in capsys.readouterr().out
    kube.check_admission.assert_not_called()
    kube.call.assert_not_called()


@pytest.mark.parametrize("override,expected", [(None, "tainted"), ("shared", "shared"), ("tainted", "tainted")])
def test_doctor_honors_config_and_flag_without_compose(tmp_path, monkeypatch, override, expected):
    (tmp_path / "podgrove.yml").write_text("node_mode: tainted\n")
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    flags = ["--node-mode", override] if override else []
    assert cli.execute(doctor_args(tmp_path, *flags)) == 0
    kube.preflight.assert_called_once_with(node_mode=expected, tainted_nodes=default_tainted_nodes())


def test_doctor_tainted_flag_works_without_config(tmp_path, monkeypatch):
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    assert cli.execute(doctor_args(tmp_path, "--node-mode", "tainted")) == 0
    kube.preflight.assert_called_once_with(node_mode="tainted", tainted_nodes=default_tainted_nodes())


def test_doctor_rejects_unknown_configuration_before_preflight(tmp_path, monkeypatch):
    (tmp_path / "podgrove.yml").write_text("node_mode: shared\nnode_mod: tainted\n")
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    with pytest.raises(PodgroveError, match="node_mod"):
        cli.execute(doctor_args(tmp_path))
    kube.preflight.assert_not_called()


def test_doctor_validates_explicit_compose_file(tmp_path, monkeypatch):
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    with pytest.raises(PodgroveError, match="compose.files"):
        cli.execute(doctor_args(tmp_path, "-f", "missing.yml"))
    kube.preflight.assert_not_called()


@pytest.mark.parametrize("configured,override,expected", [
    (None, None, "shared"), ("tainted", None, "tainted"),
    ("tainted", "shared", "shared"), ("shared", "tainted", "tainted"),
])
def test_up_propagates_effective_node_mode_to_manifests_preflight_and_state(
    project, monkeypatch, configured, override, expected,
):
    if configured:
        (project / "podgrove.yml").write_text(f"node_mode: {configured}\n")
    monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {"image": "busybox:1.37"}}})
    render = Mock(wraps=cli.manifests)
    monkeypatch.setattr(cli, "manifests", render)
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    def start(path):
        data = state.read(path)
        data["status"] = "ready"
        state.write(path, data)
    monkeypatch.setattr(runtime, "spawn", start)
    flags = ["--node-mode", override] if override else []
    assert cli.up(up_args(project, *flags), project) == 0
    assert render.call_args.kwargs["node_mode"] == expected
    kube.preflight.assert_called_once_with(node_mode=expected, tainted_nodes=default_tainted_nodes())
    assert state.read(state.state_path(project, "test-context"))["node_mode"] == expected


@pytest.mark.parametrize("stored,requested", [("shared", "tainted"), ("tainted", "shared"), (None, "shared")])
@pytest.mark.parametrize("refresh", [False, True])
def test_existing_node_mode_cannot_silently_change_even_with_refresh(project, monkeypatch, stored, requested, refresh):
    monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {"image": "busybox:1.37"}}})
    path = state.state_path(project, "test-context")
    previous = {"identity": state.identity(project), "root": str(project), "context": "test-context",
                "namespace": "podgrove-testing", "status": "ready"}
    if stored is not None:
        previous["node_mode"] = stored
    state.write(path, previous)
    control = Mock()
    monkeypatch.setattr(runtime, "control", control)
    monkeypatch.setattr(runtime, "is_running", lambda *_: True)
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    flags = ["--node-mode", requested, *(["--refresh"] if refresh else [])]
    with pytest.raises(PodgroveError, match="down.*recreate"):
        cli.up(up_args(project, *flags), project)
    assert state.read(path) == previous
    control.assert_not_called()
    kube.preflight.assert_not_called()
    kube.create_environment.assert_not_called()
    kube.destroy.assert_not_called()


def test_legacy_environment_can_be_reused_with_explicit_tainted_mode(project, monkeypatch):
    monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {"image": "busybox:1.37"}}})
    path = state.state_path(project, "test-context")
    state.write(path, {"identity": state.identity(project), "root": str(project), "context": "test-context",
                       "namespace": "podgrove-testing", "status": "ready"})
    monkeypatch.setattr(runtime, "is_running", lambda *_: True)
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    assert cli.up(up_args(project, "--node-mode", "tainted"), project) == 0
    kube.create_environment.assert_not_called()


def test_doctor_passes_custom_selector_and_toleration(tmp_path, monkeypatch):
    (tmp_path / "podgrove.yml").write_text(
        "node_mode: tainted\ntainted_nodes:\n  selector: {example.com/pool: testing}\n"
        "  taint: {key: example.com/workload, value: tests, effect: NoExecute}\n"
    )
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    assert cli.execute(doctor_args(tmp_path)) == 0
    kube.preflight.assert_called_once_with(node_mode="tainted", tainted_nodes={
        "selector": {"example.com/pool": "testing"},
        "taint": {"key": "example.com/workload", "value": "tests", "effect": "NoExecute"},
    })


@pytest.mark.parametrize("setting", [
    "selector: {pool: new}", "taint: {key: workload}", "taint: {value: new}", "taint: {effect: NoExecute}",
])
def test_refresh_cannot_change_tainted_placement_of_existing_engine(project, monkeypatch, setting):
    (project / "podgrove.yml").write_text(f"node_mode: tainted\ntainted_nodes:\n  {setting}\n")
    monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {"image": "busybox:1.37"}}})
    path = state.state_path(project, "test-context")
    previous = {"identity": state.identity(project), "root": str(project), "context": "test-context",
                "namespace": "podgrove-testing", "status": "ready", "node_mode": "tainted",
                "tainted_nodes": default_tainted_nodes()}
    state.write(path, previous)
    monkeypatch.setattr(runtime, "is_running", lambda *_: True)
    control = Mock()
    monkeypatch.setattr(runtime, "control", control)
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    with pytest.raises(PodgroveError, match="down.*recreate"):
        cli.up(up_args(project, "--refresh"), project)
    assert state.read(path) == previous
    control.assert_not_called()
    kube.destroy.assert_not_called()
    kube.create_environment.assert_not_called()


def test_old_dedicated_state_maps_to_default_tainted_placement(project, monkeypatch):
    monkeypatch.setattr(Compose, "model", lambda *_: {"services": {"app": {"image": "busybox:1.37"}}})
    path = state.state_path(project, "test-context")
    state.write(path, {"identity": state.identity(project), "root": str(project), "context": "test-context",
                       "namespace": "podgrove-testing", "status": "ready", "node_mode": "dedicated"})
    monkeypatch.setattr(runtime, "is_running", lambda *_: True)
    monkeypatch.setattr(cli, "Kube", Mock())
    assert cli.up(up_args(project, "--node-mode", "tainted"), project) == 0


def test_down_removes_local_artifacts_and_is_safe_to_repeat(project, monkeypatch):
    path = state.state_path(project, "test-context")
    state.write(path, {"identity": state.identity(project), "root": str(project), "context": "test-context",
                       "namespace": "podgrove-testing", "status": "disconnected"})
    path.with_suffix(".log").write_text("log")
    path.with_suffix(".123.tmp").write_text("temp")
    kube = Mock(namespace="podgrove-testing")
    kube.lease_mode.return_value = "shared"
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    monkeypatch.setattr(runtime, "is_running", lambda _: False)
    for _ in range(2):
        assert cli.execute(lifecycle_args(project, "down")) == 0
        assert not path.parent.exists()
    assert kube.destroy.call_args_list == [((state.identity(project),),), ((state.identity(project),),)]


def test_down_cluster_failure_keeps_binding_and_logs_then_retry_cleans(project, monkeypatch):
    path = state.state_path(project, "test-context")
    original = {"identity": state.identity(project), "root": str(project), "context": "test-context",
                "namespace": "podgrove-testing", "status": "disconnected"}
    state.write(path, original)
    path.with_suffix(".log").write_text("evidence")
    kube = Mock(namespace="podgrove-testing")
    kube.destroy.side_effect = PodgroveError("delete denied")
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    monkeypatch.setattr(runtime, "is_running", lambda _: False)
    with pytest.raises(PodgroveError, match="delete denied"):
        cli.execute(lifecycle_args(project, "down"))
    assert state.read(path) == original and path.with_suffix(".log").read_text() == "evidence"
    assert not path.with_suffix(".lock").exists()
    kube.destroy.side_effect = None
    assert cli.execute(lifecycle_args(project, "down")) == 0
    assert not path.parent.exists()


def test_missing_state_down_uses_only_configured_namespace_and_retains_failed_intent(project, monkeypatch):
    kube = Mock(namespace="my-team")
    kube.lease_mode.return_value = "shared"
    kube.destroy.side_effect = PodgroveError("unavailable")
    factory = Mock(return_value=kube)
    monkeypatch.setattr(cli, "Kube", factory)
    args = cli.parser().parse_args(["down", "--context", "test-context", "--namespace", "my-team",
                                    "--project-directory", str(project)])
    with pytest.raises(PodgroveError, match="unavailable"):
        cli.execute(args)
    assert all(call.args == ("test-context", "my-team") and call.kwargs == {"namespace_mode": "shared"}
               for call in factory.call_args_list)
    saved = state.read(state.state_path(project, "test-context"))
    assert saved["namespace"] == kube.namespace and saved["status"] == "cleanup_pending"


def test_status_all_lists_deleted_worktree_without_compose_and_redacts_private_fields(project, monkeypatch, capsys):
    path = state.state_path(project, "test-context")
    state.write(path, {"identity": state.identity(project), "root": str(project), "context": "test-context",
                       "namespace": "default", "status": "ready", "token": "secret-token",
                       "socket": "/secret/socket", "docker_host": "tcp://private:2375", "unexpected_secret": "password"})
    other = state.state_path(project, "other-context")
    state.write(other, {"identity": state.identity(project), "root": str(project), "context": "other-context",
                        "namespace": "default", "status": "ready"})
    (project / "compose.yaml").unlink()
    project.rmdir()
    monkeypatch.setattr(runtime, "is_running", lambda _: False)
    monkeypatch.setattr(cli, "Compose", Mock(side_effect=AssertionError("No Compose for all-local inventory")))
    monkeypatch.setattr(cli, "Kube", Mock(side_effect=AssertionError("No cluster calls for all-local inventory")))
    args = cli.parser().parse_args(["status", "--all", "--context", "test-context", "--namespace", "default", "--json"])
    assert cli.execute(args) == 0
    output = capsys.readouterr().out
    rows = json.loads(output)["environments"]
    assert len(rows) == 1 and rows[0]["root"] == str(project) and rows[0]["status"] == "disconnected"
    assert all(secret not in output for secret in ("secret-token", "/secret/socket", "private:2375", "password", "other-context"))


def test_status_all_reports_corrupt_scoped_state_as_error(project, capsys):
    path = state.state_path(project, "test-context")
    path.write_text("not json")
    args = cli.parser().parse_args(["status", "--all", "--context", "test-context", "--namespace", "default", "--json"])
    assert cli.execute(args) == 1
    rows = json.loads(capsys.readouterr().out)["environments"]
    assert len(rows) == 1 and rows[0]["status"] == "error"
    assert rows[0]["identity"] == state.identity(project)


def test_status_all_does_not_require_a_usable_current_working_directory(project, monkeypatch, capsys):
    monkeypatch.setattr(cli.Path, "cwd", Mock(side_effect=FileNotFoundError("worktree was deleted")))
    args = cli.parser().parse_args(["status", "--all", "--context", "test-context", "--namespace", "default", "--json"])
    assert cli.execute(args) == 0
    assert json.loads(capsys.readouterr().out) == {"environments": []}


def test_storage_failure_happens_before_namespace_creation(project, monkeypatch):
    monkeypatch.setattr(Compose, "model", lambda _: {"services": {"app": {"image": "busybox:1.37"}}})
    kube = Mock()
    kube.check_storage.side_effect = PodgroveError("StorageClass must reclaim Delete")
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    with pytest.raises(PodgroveError, match="reclaim Delete"):
        cli.up(up_args(project), project)
    kube.ensure_namespace.assert_not_called()
    kube.create_environment.assert_not_called()
    assert not state.state_path(project, "test-context").exists()


def test_targeted_reap_passes_explicit_identity(project, monkeypatch):
    reaper = Mock(return_value=[])
    monkeypatch.setattr(cli, "reap", reaper)
    monkeypatch.setattr(cli, "Kube", Mock())
    args = lifecycle_args(project, "reap", "--environment", state.identity(project), "--dry-run")
    assert cli.execute(args) == 0
    assert reaper.call_args.kwargs == {"identity": state.identity(project)}


@pytest.mark.parametrize("value,expected", [("all", "all"), ("0", 0), ("100", 100), ("100000", 100000)])
def test_logs_tail_accepts_all_retained_history_or_non_negative_counts(value, expected):
    args = cli.parser().parse_args(["logs", "api", "--tail", value])
    assert args.tail == expected


@pytest.mark.parametrize("value", ["ALL", "all ", "-1", "1.5", "true", "", "--all"])
def test_logs_invalid_tail_fails_before_dispatch(value, monkeypatch):
    kube = Mock(side_effect=AssertionError("No invalid argument cluster reads"))
    monkeypatch.setattr(cli, "Kube", kube)
    with pytest.raises(SystemExit) as caught:
        cli.parser().parse_args(["logs", "api", "--tail", value])
    assert caught.value.code == 2
    kube.assert_not_called()


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("follow", [True, False])
def test_all_logs_streams_uncapped_to_inherited_stdout_and_preserves_exit_status(project, monkeypatch, capfd,
                                                                              connected, follow):
    import os
    import sys
    from podgrove import docker_tunnel
    (project / "podgrove.yml").write_text("cluster: {context: log-fixture, namespace: owned}\n")
    monkeypatch.chdir(project)
    data = {"identity": state.identity(project), "root": str(project), "context": "log-fixture", "namespace": "owned",
            "namespace_mode": "shared", "node_mode": "shared", "status": "ready",
            "docker_host": "tcp://127.0.0.1:12345", "compose_project": "log-fixture", "compose_services": ["app"]}
    path = state.state_path(project, data["context"])
    state.write(path, data)
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    monkeypatch.setattr(runtime, "is_running", lambda _: connected)
    monkeypatch.setattr(runtime, "control", Mock(return_value={"ok": True}))
    monkeypatch.setattr(Compose, "model", lambda _: {"name": "log-fixture", "services": {"app": {}}})
    tunnel = Mock(port=23456)
    monkeypatch.setattr(docker_tunnel, "DockerTunnel", Mock(return_value=tunnel))
    fake_bin = project / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    # A real child writes more than the dashboard byte/line limits. Podgrove
    # passes stdout through directly, and does not collect the export in memory.
    docker.write_text(f"#!{sys.executable}\nimport sys\nassert '--tail' in sys.argv\n"
                      "assert sys.argv[sys.argv.index('--tail')+1]=='all'\n"
                      f"assert ('--follow' in sys.argv)=={follow!r}\n"
                      "sys.stdout.write('retained-history-line\\n' * 20000)\nsys.exit(17)\n")
    docker.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ.get("PATH", ""))
    args = cli.parser().parse_args(["logs", "app", "--tail", "all", *(["--follow"] if follow else [])])
    assert cli.execute(args) == 17
    assert capfd.readouterr().out == "retained-history-line\n" * 20000
    assert state.read(path) == data
    kube.create_environment.assert_not_called()
    if connected:
        tunnel.start.assert_not_called()
    else:
        tunnel.start.assert_called_once()
        tunnel.close.assert_called_once()
