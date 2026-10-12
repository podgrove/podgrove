"""Cleanup results stay machine-readable on success and recoverable failure."""
import json
import sys
from unittest.mock import Mock

import pytest

from podgrove import cli, runtime, state
from podgrove.errors import PodgroveError


@pytest.fixture
def cleanup_target(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    args = ["podgrove", "down", "--context", "test-context", "--namespace", "team",
            "--project-directory", str(root), "--json"]
    monkeypatch.setattr(sys, "argv", args)
    kube = Mock(namespace="team")
    kube.lease_mode.return_value = "shared"
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    monkeypatch.setattr(runtime, "stop_session", Mock())
    data = {"identity": state.identity(root), "root": str(root), "context": "test-context",
            "namespace": "team", "namespace_mode": "shared", "status": "disconnected"}
    return root, kube, data


@pytest.mark.parametrize("recorded", [False, True])
def test_down_json_and_repeated_cleanup_are_single_documents(cleanup_target, capsys, recorded):
    root, kube, data = cleanup_target
    path = state.state_path(root, "test-context")
    if recorded:
        state.write(path, {**data, "token": "private-session-token", "docker_host": "private-host"})
    for _ in range(2):
        assert cli.main() == 0
        output = capsys.readouterr()
        assert output.err == ""
        assert json.loads(output.out) == {
            "identity": data["identity"], "context": "test-context", "namespace": "team",
            "status": "removed", "namespace_retained": True, "bootstrap_retained": True}
        assert not path.exists()
    assert kube.destroy.call_count == 2


def test_down_json_failure_preserves_state_for_retry(cleanup_target, capsys):
    root, kube, data = cleanup_target
    path = state.state_path(root, "test-context")
    state.write(path, data)
    kube.destroy.side_effect = PodgroveError("delete denied")
    assert cli.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"command": "down", "status": "error", "error": "delete denied"}
    assert output.err == ""
    assert state.read(path) == data
    kube.destroy.side_effect = None
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "removed"


def test_plain_down_keeps_human_output(cleanup_target, capsys, monkeypatch):
    _, _, data = cleanup_target
    monkeypatch.setattr(sys, "argv", sys.argv[:-1])
    assert cli.main() == 0
    output = capsys.readouterr()
    assert output.out == f"Removed environment {data['identity']}; namespace and bootstrap retained\n"
    assert output.err == ""


@pytest.mark.parametrize("complete", [True, False])
def test_pending_cleanup_reports_exact_leftovers_and_preserves_retry_state(cleanup_target, capsys, complete):
    from podgrove.kube import CleanupPending
    root, kube, data = cleanup_target
    path = state.state_path(root, "test-context")
    state.write(path, data)
    path.with_suffix(".log").write_text("startup evidence")
    remaining = [{"kind": "Pod", "name": "pg-" + data["identity"] + "-0", "uid": "old-pod-uid",
                  "deleting": True, "finalizers": ["example/protection"]}]
    kube.destroy.side_effect = CleanupPending("Cleanup deadline reached", remaining, complete=complete, observed_at=123)
    assert cli.main() == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == ""
    assert result["status"] == "cleanup_pending"
    assert result["remaining"] == remaining
    assert result["inventory_complete"] is complete
    saved = state.read(path)
    assert saved["cleanup"]["remaining"] == remaining
    assert saved["status"] == "cleanup_pending"
    assert path.with_suffix(".log").read_text() == "startup evidence"
    kube.destroy.side_effect = None
    assert cli.main() == 0
    assert not path.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "removed"


def test_down_passes_explicit_cleanup_budget(cleanup_target, monkeypatch, capsys):
    _, kube, data = cleanup_target
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--timeout", "2.5"])
    assert cli.main() == 0
    assert kube.destroy.call_args.args == (data["identity"],)
    assert 0 < kube.destroy.call_args.kwargs["timeout"] <= 2.5
    kube.get.assert_called_once_with("configmap", "pg-" + data["identity"], timeout=2.5)
    assert json.loads(capsys.readouterr().out)["status"] == "removed"


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "broken"])
def test_down_rejects_invalid_cleanup_budget_before_cluster(cleanup_target, monkeypatch, capsys, value):
    _, kube, _ = cleanup_target
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--timeout", value])
    with pytest.raises(SystemExit) as captured:
        cli.main()
    assert captured.value.code == 2
    kube.destroy.assert_not_called()
    assert "positive finite seconds" in capsys.readouterr().err


@pytest.mark.parametrize("phase", ["read", "validation"])
def test_stateless_cleanup_rejects_late_lease_before_destroy(cleanup_target, monkeypatch, capsys, phase):
    from types import SimpleNamespace
    _, kube, _ = cleanup_target
    clock = [0]
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--timeout", "1"])
    def read(*args, **kwargs):
        assert kwargs["timeout"] == 1
        if phase == "read":
            clock[0] = 5
        return {}
    def mode(ident, lease):
        assert lease == {}
        if phase == "validation":
            clock[0] = 5
        return "shared"
    kube.get.side_effect, kube.lease_mode.side_effect = read, mode
    assert cli.main() == 1
    assert "deadline" in json.loads(capsys.readouterr().out)["error"]
    kube.destroy.assert_not_called()
    if phase == "read":
        kube.lease_mode.assert_not_called()


def test_stateless_cleanup_subtracts_lease_read_and_validation_from_one_budget(cleanup_target, monkeypatch, capsys):
    from types import SimpleNamespace
    _, kube, data = cleanup_target
    clock = [0]
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--timeout", "2.5"])
    def read(*args, **kwargs):
        assert kwargs["timeout"] == 2.5
        clock[0] += .75
        return {}
    def mode(ident, lease):
        assert lease == {}
        clock[0] += .25
        return "shared"
    kube.get.side_effect, kube.lease_mode.side_effect = read, mode
    assert cli.main() == 0
    kube.destroy.assert_called_once_with(data["identity"], timeout=1.5)
    assert json.loads(capsys.readouterr().out)["status"] == "removed"
