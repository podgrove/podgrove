"""Identical committed YAML in real disposable linked Git worktrees."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from unittest.mock import Mock

import pytest

from podgrove import cli, state
from podgrove.compose import Compose
from podgrove.config import load_config
from podgrove.forward import port_plan
from podgrove.kube import resolve_namespace
from podgrove.repository import worktree_root


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "portable"


def git(directory, *arguments):
    # Only this test's disposable repository is touched. Ignore caller Git
    # overrides/config/hooks/signing and never run a remote operation.
    environment = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_AUTHOR_NAME": "Podgrove fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                   "GIT_COMMITTER_NAME": "Podgrove fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                   "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(directory), "-c", "core.hooksPath=/dev/null",
                           "-c", "commit.gpgsign=false", *arguments],
                          env=environment, check=True, capture_output=True, timeout=20).stdout


@pytest.mark.parametrize("mode", ["shared", "worktree"])
def test_committed_yaml_is_unchanged_across_real_linked_worktrees(tmp_path, monkeypatch, capsys, mode):
    if not shutil.which("git") or not shutil.which("docker"):
        pytest.skip("Git and Docker Compose CLI are required; no Docker daemon or Kubernetes is used")
    main = tmp_path / "source"
    shutil.copytree(EXAMPLE, main)
    config_path = main / "podgrove.yml"
    config_path.write_text(config_path.read_text().replace("namespace_mode: shared", f"namespace_mode: {mode}"))
    git(main, "-c", "init.templateDir=", "init", "--initial-branch=main")
    git(main, "add", "podgrove.yml", "backend", "README.md")
    git(main, "commit", "-m", "Disposable portable configuration fixture")
    committed = git(main, "show", "HEAD:podgrove.yml")
    seats = [tmp_path / name for name in ("seat-a", "seat-b")]
    for seat in seats:
        git(main, "worktree", "add", "-b", seat.name, str(seat))
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    kube = Mock(side_effect=AssertionError("Portable configuration validation must remain offline"))
    monkeypatch.setattr(cli, "Kube", kube)
    identities, namespaces, local_ports, service_sources = [], [], [], []
    with ExitStack() as cleanup:
        for seat in seats:
            assert (seat / ".git").is_file()  # Actual linked-worktree metadata, not a standalone copy.
            assert worktree_root(seat / "backend" / "public") == seat
            assert (seat / "podgrove.yml").read_bytes() == committed
            config = load_config(seat)
            assert config.root == seat and config.project_directory == seat / "backend"
            compose = Compose(config)
            model = compose.model()  # Real Compose normalization, no engine start.
            service_sources.append(model["services"]["web"]["volumes"][0]["source"])
            assert service_sources[-1] == str(seat / "backend" / "public")
            ident = state.identity(seat)
            for directory in (seat, seat / "backend", seat / "backend" / "public"):
                arguments = cli.parser().parse_args(["up", "--dry-run", "--json",
                                                     "--project-directory", str(directory)])
                assert cli.execute(arguments) == 0
                plan = json.loads(capsys.readouterr().out)
                assert plan["identity"] == ident
                assert plan["namespace"] == resolve_namespace(config.namespace, mode, ident)
                assert all(resource["metadata"]["labels"]["podgrove.dev/environment"] == ident
                           for resource in plan["resources"])
            rows = [{"Service": "web", "Project": model["name"], "State": "running", "Publishers": [
                {"URL": "0.0.0.0", "TargetPort": 8080, "PublishedPort": 49155, "Protocol": "tcp"}]}]
            endpoint = port_plan(compose.published_ports(model), config.forward, ident,
                                 observed=rows, project=model["name"])[0]
            listener = cleanup.enter_context(socket.socket())
            listener.bind(("127.0.0.1", endpoint["local"]))
            listener.listen()
            local_ports.append(endpoint["local"])
            identities.append(ident)
            namespaces.append(plan["namespace"])
            assert git(seat, "status", "--porcelain") == b""
            assert (seat / "podgrove.yml").read_bytes() == committed
    assert len(set(identities)) == len(set(local_ports)) == len(set(service_sources)) == 2
    assert len(set(namespaces)) == (1 if mode == "shared" else 2)
    assert state.state_path(seats[0], config.context) != state.state_path(seats[1], config.context)
    kube.assert_not_called()
