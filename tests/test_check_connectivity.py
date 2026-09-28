"""Offline guards and actual loopback probes for the opt-in live harness."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location("check_connectivity", Path(__file__).resolve().parents[1] / "scripts/check_connectivity.py")
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


@pytest.fixture
def args(tmp_path):
    return SimpleNamespace(output=tmp_path / "evidence", podgrove_bin=Path(sys.executable), context="explicit-context",
                           namespace="approved-tests", storage_class="approved-storage")


def object_list():
    return {"items": [{"kind": "Pod", "metadata": {"name": "pg-012345abcdef-0", "namespace": "approved-tests",
                       "uid": "captured-pod", "labels": {acceptance.MANAGED: "podgrove", acceptance.ENVIRONMENT: "012345abcdef"}}}]}


def test_exact_ownership_inventory_accepts_only_named_environment():
    assert acceptance.checked_objects(object_list(), "approved-tests", "012345abcdef") == {"Pod/pg-012345abcdef-0": "captured-pod"}


@pytest.mark.parametrize("mutation", ["namespace", "name", "uid", "labels", "kind", "replaced"])
def test_cleanup_inventory_fails_closed_for_foreign_or_changed_resource(mutation):
    value = object_list()
    if mutation == "kind":
        value["items"][0]["kind"] = "Node"
    elif mutation == "replaced":
        value["items"][0]["metadata"]["uid"] = "new-pod"
    else:
        value["items"][0]["metadata"][mutation] = {} if mutation == "labels" else "wrong"
        if mutation == "uid":
            value["items"][0]["metadata"][mutation] = ""
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.checked_objects(value, "approved-tests", "012345abcdef", {"Pod/pg-012345abcdef-0": "captured-pod"})


def test_actual_loopback_http_probe_confirms_bytes_and_wrong_body_fails():
    server = acceptance.LoopbackServer(b"fixture\x00\xff", "nonce")
    try:
        for body, success in ((b"fixture\x00\xff", True), (b"wrong", False)):
            program = acceptance.probe_program("127.0.0.1", server.port, expected=body, path="/nonce")
            result = subprocess.run([sys.executable, "-I", "-B", "-c", program], capture_output=True, timeout=5)
            assert (result.returncode == 0) is success
            if success:
                assert json.loads(result.stdout)["matched"]
    finally:
        server.close()


def test_refused_connection_is_not_misreported_as_policy_isolation():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    program = acceptance.probe_program("127.0.0.1", port)
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program], capture_output=True, timeout=5)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"blocked": False, "reason": "ConnectionRefusedError"}


def test_generated_fixtures_have_exact_small_budgets_and_target_only_ports():
    config = acceptance.fixture_config("ctx", "team", "fast")
    assert config["resources"] == {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}}
    assert config["storage"] == {"size": "2Gi"}
    assert config["cluster"] == {"context": "ctx", "namespace": "team", "namespace_mode": "shared", "storage_class": "fast"}
    target = acceptance.fixture_compose("target", b"hello")["services"]["gateway"]
    assert target["ports"] == ["8080", "8081"]
    assert not any(key in target for key in ("volumes", "privileged", "network_mode"))
    compile(target["command"][-1], "generated-target", "exec")
    for role in ("source", "third"):
        assert acceptance.fixture_compose(role, b"hello")["services"]["client"]["image"] == "python:3.12-alpine"


def test_existing_output_race_never_overwrites_foreign_evidence(args):
    args.output.mkdir()
    (args.output / "result.json").write_text("existing")
    runner = acceptance.Runner(args)
    assert runner.run() == 1
    assert (args.output / "result.json").read_text() == "existing"
    assert not runner.output_created


def test_cleanup_attempts_all_three_when_one_down_fails_and_preserves_diagnostics(args, tmp_path, monkeypatch):
    args.output.mkdir()
    runner = acceptance.Runner(args)
    calls = []
    for name in ("target", "source", "third"):
        root, state = tmp_path / name, tmp_path / (name + "-state")
        root.mkdir()
        state.mkdir()
        for filename in ("podgrove.yml", "compose.yml"):
            (root / filename).write_text("{}")
        (state / "session.log").write_text("preserved before cleanup")
        runner.fixtures.append({"role": name, "identity": acceptance.identity(root), "root": root,
                                "state": state, "attempted": True})
    monkeypatch.setattr(runner, "inventory", lambda role: ({"items": []}, {}))
    monkeypatch.setattr(runner, "verify_exact_absence", lambda role, captured: None)
    def cli(role, command, *args, **kwargs):
        calls.append((role["role"], command))
        if command == "status":
            raise acceptance.AcceptanceError("diagnostic unavailable")
        if role["role"] == "third":
            raise acceptance.AcceptanceError("delete denied")
        return json.dumps({"identity": role["identity"], "status": "removed", "namespace_retained": True, "bootstrap_retained": True}), 0
    monkeypatch.setattr(runner, "cli", cli)
    runner.cleanup()
    assert [role for role, cmd in calls if cmd == "down"] == ["third", "source", "target"]
    assert not runner.result["cleanup"]["third"]["passed"]
    assert runner.result["cleanup"]["source"]["passed"] and runner.result["cleanup"]["target"]["passed"]
    for role in runner.fixtures:
        assert (args.output / (role["role"] + "-session.log")).read_text() == "preserved before cleanup"


def test_inventory_uses_explicit_context_namespace_and_conjunctive_selector(args, monkeypatch):
    runner = acceptance.Runner(args)
    command = Mock(return_value=(json.dumps({"items": []}), 0))
    monkeypatch.setattr(runner, "command", command)
    role = {"identity": "012345abcdef"}
    assert runner.inventory(role) == ({"items": []}, {})
    argv = command.call_args.args[0]
    assert argv[:5] == ["kubectl", "--context", "explicit-context", "--namespace", "approved-tests"]
    assert argv[argv.index("-l") + 1] == "app.kubernetes.io/managed-by=podgrove,podgrove.dev/environment=012345abcdef"
    assert "namespace" not in argv and "node" not in argv and "--all-namespaces" not in argv


def test_command_timeout_reaps_its_own_local_child_and_records_failure(args):
    args.output.mkdir()
    runner = acceptance.Runner(args)
    runner.base = args.output
    with pytest.raises(subprocess.TimeoutExpired):
        runner.command([sys.executable, "-I", "-B", "-c", "import time;time.sleep(30)"], timeout=.1)
    assert runner.commands[0]["returncode"] != 0
    assert runner.commands[0]["finished_at"] >= runner.commands[0]["started_at"]


def test_cli_environment_uses_fresh_state_and_inherits_only_explicit_process_environment(args, tmp_path, monkeypatch):
    args.output.mkdir()
    root, state = tmp_path / "fresh", tmp_path / "private-state"
    root.mkdir()
    state.mkdir()
    monkeypatch.setenv("KUBECONFIG", "/private/inherited-config")
    monkeypatch.setenv("PODGROVE_STATE_HOME", "/other/agents/state")
    runner = acceptance.Runner(args)
    role = {"role": "source", "root": root, "state": state}
    program = "import json,os;print(json.dumps({k:os.environ[k] for k in ('KUBECONFIG','PODGROVE_STATE_HOME')}))"
    result, code = runner.command([sys.executable, "-I", "-B", "-c", program], role=role)
    assert code == 0 and json.loads(result) == {"KUBECONFIG": "/private/inherited-config", "PODGROVE_STATE_HOME": str(state)}
    assert os.environ["PODGROVE_STATE_HOME"] == "/other/agents/state"


def test_execution_gate_precedes_runner_creation(args, monkeypatch):
    runner = Mock()
    monkeypatch.setattr(acceptance, "Runner", runner)
    with pytest.raises(SystemExit):
        acceptance.main(["--podgrove-bin", str(args.podgrove_bin), "--context", args.context,
                         "--namespace", args.namespace, "--storage-class", args.storage_class, "--output", str(args.output)])
    runner.assert_not_called()


def test_exact_absence_checks_base_and_captured_link_names_without_delete(args, monkeypatch):
    runner = acceptance.Runner(args)
    command = Mock(return_value=("", 0))
    monkeypatch.setattr(runner, "command", command)
    runner.verify_exact_absence({"identity": "012345abcdef"}, {"NetworkPolicy/pg-012345abcdef-link-123456789a-in": "uid"})
    calls = [call.args[0] for call in command.call_args_list]
    assert len(calls) == len(acceptance.ALLOWED_KINDS) + 1
    assert all(call[:5] == ["kubectl", "--context", "explicit-context", "--namespace", "approved-tests"] for call in calls)
    assert all("get" in call and "delete" not in call and "--ignore-not-found" in call for call in calls)
    command.return_value = (json.dumps({"metadata": {"labels": {}}}), 0)
    with pytest.raises(acceptance.AcceptanceError, match="exact fixture"):
        runner.verify_exact_absence({"identity": "012345abcdef"}, {})


def test_ready_supervisor_pid_must_belong_to_exact_fixture_state(args, tmp_path, monkeypatch):
    runner = acceptance.Runner(args)
    root, state = tmp_path / "root", tmp_path / "state"
    root.mkdir()
    state.mkdir()
    role = {"role": "source", "identity": acceptance.identity(root), "root": root, "state": state}
    monkeypatch.setattr(runner, "cli", Mock(return_value=("{}", 0)))
    monkeypatch.setattr(runner, "status", Mock(return_value={"pid": 9876}))
    monkeypatch.setattr(runner, "command", Mock(return_value=("another agent process", 0)))
    with pytest.raises(acceptance.AcceptanceError, match="exact private state"):
        runner.start(role)
    assert role["attempted"] and not role.get("processes")
