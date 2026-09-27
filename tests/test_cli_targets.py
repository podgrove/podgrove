"""Public target selection works from config-only roots without external commands."""
from __future__ import annotations

import json
import sys
from unittest.mock import Mock

import pytest

from podgrove import cli, runtime, state, web


@pytest.fixture
def target_root(tmp_path, monkeypatch):
    root = (tmp_path / "project").resolve()
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.delenv("PODGROVE_CONTEXT", raising=False)
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "private-state"))
    forbidden = Mock(side_effect=AssertionError("Target selection must not launch an external command"))
    monkeypatch.setattr(cli.subprocess, "run", forbidden)
    monkeypatch.setattr(cli.subprocess, "call", forbidden)
    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    monkeypatch.setattr(cli, "Compose", Mock(side_effect=AssertionError("No Compose access for target selection")))
    return root


def invoke(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["podgrove", *arguments])
    return cli.main()


def configure(root, *, context="config-context", namespace="default", path="podgrove.yml", extra=""):
    fields = "cluster:\n"
    if context is not None:
        fields += f"  context: {context}\n"
    if namespace is not None:
        fields += f"  namespace: {namespace}\n"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fields + extra)
    return target


def fake_kube(monkeypatch):
    clients = []

    def create(context, namespace, *, namespace_mode=None):
        result = Mock(context=context, namespace=namespace, namespace_mode=namespace_mode)
        result.get.return_value = {"metadata": {"name": namespace}}
        result.lease_mode.return_value = namespace_mode
        clients.append(result)
        return result

    monkeypatch.setattr(cli, "Kube", create)
    return clients


def test_bare_web_uses_config_only_root_without_compose_or_state(target_root, monkeypatch):
    configure(target_root)
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    monkeypatch.setattr(cli, "Kube", Mock(side_effect=AssertionError("No cluster client before web serving")))
    assert invoke(monkeypatch, "web") == 0
    serve.assert_called_once_with("config-context", port=0, namespace="default", open_browser=True)
    assert not (target_root / "compose.yml").exists()
    assert not (target_root.parent / "private-state").exists()


def test_web_does_not_open_referenced_compose_or_env_inputs(target_root, monkeypatch):
    configure(target_root, extra="compose:\n  files: [absent-compose.yml]\n  env_file: absent.env\n")
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    assert invoke(monkeypatch, "web", "--no-open") == 0
    serve.assert_called_once_with("config-context", port=0, namespace="default", open_browser=False)


def test_web_accepts_custom_config_relative_to_selected_project(target_root, monkeypatch, tmp_path):
    configure(target_root, path="settings/dashboard.yml")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    assert invoke(monkeypatch, "web", "--project-directory", str(target_root),
                  "--config", "settings/dashboard.yml", "--port", "8765", "--no-open") == 0
    serve.assert_called_once_with("config-context", port=8765, namespace="default", open_browser=False)


@pytest.mark.parametrize("flags, expected_context, expected_namespace", [
    ([], "config-context", "default"),
    (["--context", "flag-context"], "flag-context", "default"),
    (["--namespace", "podgrove-testing"], "config-context", "podgrove-testing"),
    (["--context", "flag-context", "--namespace", "podgrove-testing"], "flag-context", "podgrove-testing"),
])
def test_each_explicit_flag_overrides_yaml_and_yaml_overrides_environment(
    target_root, monkeypatch, flags, expected_context, expected_namespace
):
    configure(target_root)
    monkeypatch.setenv("PODGROVE_CONTEXT", "environment-context")
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    assert invoke(monkeypatch, "web", *flags) == 0
    assert serve.call_args.args == (expected_context,)
    assert serve.call_args.kwargs["namespace"] == expected_namespace


@pytest.mark.parametrize("configured_namespace", ["my-team", "default"])
def test_environment_context_is_fallback_without_yaml_context(target_root, monkeypatch, configured_namespace):
    configure(target_root, context=None, namespace=configured_namespace)
    monkeypatch.setenv("PODGROVE_CONTEXT", "environment-context")
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    assert invoke(monkeypatch, "web") == 0
    assert serve.call_args.args == ("environment-context",)
    assert serve.call_args.kwargs["namespace"] == configured_namespace


@pytest.mark.parametrize("text", [
    "cluster: [broken\n",
    "cluster:\n  context: 42\n",
    "cluster:\n  context: valid\n  namespace: ../default\n",
    "cluster:\n  context: one\n  context: two\n",
    "cluster:\n  context: valid\n  typo: wrong\n",
])
def test_malformed_config_refused_before_binding_even_with_flags(target_root, monkeypatch, capsys, text):
    (target_root / "podgrove.yml").write_text(text)
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    assert invoke(monkeypatch, "web", "--context", "flag-context") == 1
    serve.assert_not_called()
    assert "podgrove:" in capsys.readouterr().err


@pytest.mark.parametrize("text", [None, "cluster:\n  namespace: default\n"])
def test_missing_context_refused_before_binding_or_implicit_context_lookup(target_root, monkeypatch, capsys, text):
    if text:
        (target_root / "podgrove.yml").write_text(text)
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    assert invoke(monkeypatch, "web") == 1
    serve.assert_not_called()
    assert ("context" if text else "namespace") in capsys.readouterr().err.lower()


def test_explicit_missing_config_refused_before_binding(target_root, monkeypatch):
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    assert invoke(monkeypatch, "web", "--context", "flag-context", "--config", "missing.yml") == 1
    serve.assert_not_called()


def test_status_all_uses_configured_inventory_scope_without_compose_or_kube(target_root, monkeypatch, capsys):
    configure(target_root)
    records = Mock(return_value=[])
    monkeypatch.setattr(state, "local_records", records)
    monkeypatch.setattr(cli, "Kube", Mock(side_effect=AssertionError("Local inventory needs no cluster client")))
    assert invoke(monkeypatch, "status", "--all", "--json") == 0
    records.assert_called_once_with("config-context", "default")
    assert json.loads(capsys.readouterr().out) == {"environments": []}


def test_doctor_uses_config_target_without_compose_access(target_root, monkeypatch):
    configure(target_root)
    clients = fake_kube(monkeypatch)
    assert invoke(monkeypatch, "doctor") == 0
    assert clients[-1].context == "config-context" and clients[-1].namespace == "default"
    clients[-1].preflight.assert_called_once()
    clients[-1].check_admission.assert_called_once()
    clients[-1].create_environment.assert_not_called()


def test_reap_accepts_yaml_namespace_without_broad_discovery(target_root, monkeypatch):
    configure(target_root)
    clients = fake_kube(monkeypatch)
    reaper = Mock(return_value=[])
    monkeypatch.setattr(cli, "reap", reaper)
    assert invoke(monkeypatch, "reap", "--dry-run") == 0
    assert clients[-1].context == "config-context" and clients[-1].namespace == "default"
    reaper.assert_called_once_with(clients[-1], True, identity=state.identity(target_root))


def recorded_environment(root):
    data = {"identity": state.identity(root), "root": str(root), "context": "config-context",
            "namespace": "default", "status": "disconnected"}
    path = state.state_path(root, data["context"])
    state.write(path, data)
    return path, data


@pytest.mark.parametrize("command, suffix", [
    ("status", ["--json"]), ("logs", ["api"]), ("exec", ["api", "--", "true"]), ("down", []),
])
def test_recorded_namespace_survives_current_yaml_namespace_change(target_root, monkeypatch, command, suffix):
    configure(target_root, namespace="podgrove-testing")
    path, original = recorded_environment(target_root)
    clients = fake_kube(monkeypatch)
    monkeypatch.setattr(runtime, "is_running", Mock(return_value=False))
    stop = Mock()
    monkeypatch.setattr(runtime, "stop_session", stop)
    result = invoke(monkeypatch, command, *suffix)
    assert clients[-1].namespace == "default"
    if command == "down":
        assert result == 0
        stop.assert_called_once_with(original)
        clients[-1].destroy.assert_called_once_with(original["identity"])
        assert not path.exists()
    else:
        assert result == 1
        stop.assert_not_called()
        assert state.read(path) == original
        for client in clients:
            client.destroy.assert_not_called()


def test_explicit_namespace_mismatch_still_refuses_recorded_cleanup(target_root, monkeypatch):
    configure(target_root, namespace="podgrove-testing")
    path, original = recorded_environment(target_root)
    clients = fake_kube(monkeypatch)
    stop = Mock()
    monkeypatch.setattr(runtime, "stop_session", stop)
    assert invoke(monkeypatch, "down", "--namespace", "podgrove-testing") == 1
    stop.assert_not_called()
    assert state.read(path) == original
    for client in clients:
        client.destroy.assert_not_called()


def test_new_up_dry_run_uses_configured_namespace_without_cluster_client(target_root, monkeypatch, capsys):
    configure(target_root)
    (target_root / "compose.yml").write_text("services:\n  api:\n    image: busybox:1.37\n")
    compose = Mock()
    compose.model.return_value = {"services": {"api": {"image": "busybox:1.37"}}}
    monkeypatch.setattr(cli, "Compose", Mock(return_value=compose))
    monkeypatch.setattr(cli, "Kube", Mock(side_effect=AssertionError("Dry-run must not create a cluster client")))
    assert invoke(monkeypatch, "up", "--dry-run", "--json") == 0
    assert json.loads(capsys.readouterr().out)["namespace"] == "default"


@pytest.mark.parametrize("command", ["down", "status"])
def test_explicit_target_permits_existing_record_recovery_with_broken_default_yaml(
    target_root, monkeypatch, command
):
    (target_root / "podgrove.yml").write_text("cluster: [broken\n")
    path, original = recorded_environment(target_root)
    clients = fake_kube(monkeypatch)
    monkeypatch.setattr(runtime, "is_running", Mock(return_value=False))
    monkeypatch.setattr(runtime, "stop_session", Mock())
    result = invoke(monkeypatch, command, "--context", "config-context", "--namespace", "default")
    assert clients[-1].namespace == "default"
    if command == "down":
        assert result == 0 and not path.exists()
        clients[-1].destroy.assert_called_once_with(original["identity"])
    else:
        assert result == 1 and state.read(path) == original
        clients[-1].destroy.assert_not_called()


def test_explicit_custom_config_is_checked_even_when_target_flags_complete(target_root, monkeypatch):
    (target_root / "broken.yml").write_text("cluster: [broken\n")
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    assert invoke(monkeypatch, "web", "--context", "flag-context", "--namespace", "default",
                  "--config", "broken.yml") == 1
    serve.assert_not_called()
