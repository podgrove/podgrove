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
