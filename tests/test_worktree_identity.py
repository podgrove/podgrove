"""Worktree identity is independent from invocation and Compose directories."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from podgrove import cli, repository, runtime, state, web
from podgrove.compose import Compose
from podgrove.config import load_config
from podgrove.errors import PodgroveError
from podgrove.kube import resolve_namespace
from test_runtime import supervised_session as supervised_session


def checkout(path, *, common=None):
    path.mkdir(parents=True)
    if common is None:
        metadata = path / ".git"
        metadata.mkdir()
    else:
        metadata = common / "worktrees" / path.name
        metadata.mkdir(parents=True)
        (metadata / "commondir").write_text("../..\n")
        (path / ".git").write_text(f"gitdir: {metadata}\n")
    (metadata / "HEAD").write_text("ref: refs/heads/feature/example\n")
    return path


def config_at(path, mode="shared"):
    (path / "podgrove.yml").write_text(
        f"cluster: {{context: offline-identity, namespace: developer, namespace_mode: {mode}}}\n"
        "compose: {files: [compose.yml]}\nforward: []\n")
    (path / "compose.yml").write_text("services: {app: {image: busybox:1.37}}\n")


def invoke(command, path, *options):
    return cli.execute(cli.parser().parse_args([command, "--project-directory", str(path), *options]))


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "capture_anchor", lambda *_: {"fixture": "owned-controller-and-pvc"})
    root = checkout(tmp_path / "repo")
    (root / "backend" / "src").mkdir(parents=True)
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    config_at(root)
    model = {"name": "fixture", "services": {"app": {"image": "busybox:1.37"}}}
    monkeypatch.setattr(Compose, "model", lambda _: deepcopy(model))
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    created = []
    def ready(path):
        created.append(path)
        data = state.read(path)
        data.update(status="ready", ports=[], compose_services=["app"])
        state.write(path, data)
    monkeypatch.setattr(runtime, "spawn", ready)
    monkeypatch.setattr(runtime, "is_running", lambda *_: True)
    monkeypatch.setattr(runtime, "control", lambda *_: {"ok": True, "status": "ready",
                                                       "forward_status": {"state": "disabled"}})
    return root, kube, created


def test_top_level_nested_cwd_and_explicit_project_directory_share_one_environment(project, monkeypatch, capsys):
    root, kube, created = project
    roots = [root, root / "backend", root / "backend" / "src"]
    for directory in roots:
        assert invoke("up", directory, "--json") == 0
        data = json.loads(capsys.readouterr().out)
        assert data["root"] == data["config_root"] == str(root)
        assert data["identity"] == state.identity(root)
        assert data["config_path"] == str(root / "podgrove.yml")
    monkeypatch.chdir(roots[-1])
    assert cli.execute(cli.parser().parse_args(["up", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["identity"] == state.identity(root)
    assert created == [state.state_path(root, "offline-identity")]
    assert kube.create_environment.call_count == 1


def test_subdirectory_config_and_compose_base_are_saved_separately(project, capsys):
    root, _, _ = project
    nested = root / "backend"
    config_at(nested)
    assert invoke("up", nested / "src", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["root"] == str(root) and data["config_root"] == str(nested)
    assert data["config_path"] == str(nested / "podgrove.yml")
    assert data["files"] == [str(nested / "compose.yml")]
    loaded = load_config(state.configuration_root(data), Path(data["config_path"]), data["files"])
    assert loaded.root == loaded.project_directory == nested
    metadata = web.configuration_metadata(data)
    assert metadata["status"] == "available" and metadata["settings"]["compose"]["files"] == ["compose.yml"]


def test_saved_subdirectory_configuration_used_for_status_from_top_level(project, monkeypatch, capsys):
    root, _, _ = project
    config_at(root / "backend")
    invoke("up", root / "backend", "--json")
    capsys.readouterr()
    path = state.state_path(root, "offline-identity")
    data = state.read(path)
    data["docker_host"] = "tcp://127.0.0.1:34567"
    state.write(path, data)
    loader = Mock(wraps=cli.load_config)
    monkeypatch.setattr(cli, "load_config", loader)
    monkeypatch.setattr(runtime, "service_status", lambda *_: [{"Service": "app", "State": "running"}])
    assert invoke("status", root, "--json") == 0
    assert loader.call_args.args[0] == root / "backend"
    assert json.loads(capsys.readouterr().out)["identity"] == state.identity(root)


@pytest.mark.parametrize("supervised_session", [{"config_subdirectory": "backend"}], indirect=True)
def test_supervisor_reloads_saved_config_boundary_not_identity_root(supervised_session):
    session = supervised_session
    assert session.config_loader.call_args.args[0] == Path(session.data["root"]) / "backend"
    assert session.data["root"] != session.data["config_root"]
    assert runtime.control(session.data, "ping")["ok"]


def test_worktree_namespace_and_offline_bootstrap_use_same_top_level_identity(project, tmp_path, capsys):
    root, kube, _ = project
    config_at(root, mode="worktree")
    expected = resolve_namespace("developer", "worktree", state.identity(root))
    output = tmp_path / "bootstrap"
    assert invoke("bootstrap", root / "backend" / "src", "--output", str(output)) == 0
    assert expected in capsys.readouterr().out
    marker = json.dumps([p.read_text() for p in output.glob("*.yaml")])
    assert expected in marker and state.identity(root) in marker
    assert invoke("up", root / "backend", "--dry-run", "--json") == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["namespace"] == expected and rendered["identity"] == state.identity(root)
    kube.create_environment.assert_not_called()


def test_git_environment_never_redirects_identity_and_lookup_runs_no_commands(tmp_path, monkeypatch):
    root = checkout(tmp_path / "repo")
    subdir = root / "nested"
    subdir.mkdir()
    monkeypatch.setenv("GIT_DIR", "/unrelated/git")
    monkeypatch.setenv("GIT_WORK_TREE", "/unrelated/worktree")
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("No Git commands"))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("No Git commands"))
    assert repository.worktree_root(subdir) == root
    assert state.identity(subdir) != state.identity(root)  # Durable-state hashing remains pure.


def test_symlinked_invocation_path_resolves_to_the_same_checkout(project, tmp_path, capsys):
    root, _, _ = project
    alias = tmp_path / "alias"
    alias.symlink_to(root / "backend", target_is_directory=True)
    assert invoke("up", alias, "--json") == 0
    assert json.loads(capsys.readouterr().out)["identity"] == state.identity(root)


def test_down_from_subdirectory_removes_only_the_top_level_environment(project, monkeypatch, capsys):
    root, kube, _ = project
    assert invoke("up", root, "--json") == 0
    capsys.readouterr()
    stop = Mock()
    monkeypatch.setattr(runtime, "stop_session", stop)
    assert invoke("down", root / "backend" / "src", "--json") == 0
    assert stop.call_args.args[0]["identity"] == state.identity(root)
    kube.destroy.assert_called_once_with(state.identity(root))
    assert not state.state_path(root, "offline-identity").exists()


def test_web_and_scoped_reap_resolve_same_worktree_namespace(project, monkeypatch):
    root, _, _ = project
    config_at(root, mode="worktree")
    expected = resolve_namespace("developer", "worktree", state.identity(root))
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    assert invoke("web", root / "backend", "--no-open") == 0
    serve.assert_called_once_with("offline-identity", port=0, namespace=expected, open_browser=False)
    reap = Mock(return_value=[])
    monkeypatch.setattr(cli, "reap", reap)
    assert invoke("reap", root / "backend", "--dry-run") == 0
    assert reap.call_args.kwargs == {"identity": state.identity(root)}


def test_linked_worktrees_share_metadata_but_never_identity(tmp_path):
    common = tmp_path / "common.git"
    roots = [checkout(tmp_path / name, common=common) for name in ("seat-one", "seat-two")]
    for root in roots:
        (root / "backend").mkdir()
        assert repository.worktree_root(root / "backend") == root
    assert state.identity(roots[0]) != state.identity(roots[1])
    assert repository.repository_labels(roots[0])["repo"] == repository.repository_labels(roots[1])["repo"]


def test_nested_checkout_uses_nearest_boundary_and_not_parent_configuration(tmp_path):
    outer = checkout(tmp_path / "outer")
    config_at(outer)
    inner = checkout(outer / "inner")
    child = inner / "child"
    child.mkdir()
    assert repository.worktree_root(child) == inner
    assert repository.configuration_root(child, inner) == child


def test_standalone_copy_and_missing_checkout_keep_selected_identity(tmp_path):
    (tmp_path / "podgrove.yml").write_text("outside config must not be discovered\n")
    plain = tmp_path / "copy"
    plain.mkdir()
    assert repository.worktree_root(plain) == plain
    assert repository.configuration_root(plain, plain) == plain
    missing = tmp_path / "removed"
    assert repository.worktree_root(missing) == missing


@pytest.mark.parametrize("case", ["invalid-file", "dangling-pointer", "symlink", "fifo"])
def test_ambiguous_git_boundary_fails_closed(tmp_path, case):
    root = checkout(tmp_path / "outer")
    nested = root / "nested"
    nested.mkdir()
    marker = nested / ".git"
    if case == "invalid-file":
        marker.write_text("invalid")
    elif case == "dangling-pointer":
        marker.write_text("gitdir: missing\n")
    elif case == "symlink":
        marker.symlink_to(root / ".git", target_is_directory=True)
    else:
        import os
        os.mkfifo(marker)
    with pytest.raises(PodgroveError, match="ambiguous identity"):
        repository.worktree_root(nested)


def test_explicit_config_preserves_selected_base_and_external_restrictions(project, capsys):
    root, _, _ = project
    nested = root / "backend"
    config_at(nested)
    (nested / "podgrove.yml").rename(nested / "alternate.yml")
    assert invoke("up", nested, "--config", "alternate.yml", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["root"] == str(root) and data["config_root"] == str(nested)
    assert data["config_path"] == str(nested / "alternate.yml")
    with pytest.raises(PodgroveError, match="outside the worktree"):
        invoke("validate", nested, "--config", "../podgrove.yml")


@pytest.mark.parametrize("value", ["relative", None, "/outside/config"])
def test_invalid_saved_config_root_is_rejected_before_file_reads(tmp_path, value):
    record = {"root": str(tmp_path), "config_root": value}
    with pytest.raises(PodgroveError, match="config_root|outside the worktree"):
        state.configuration_root(record)
    assert web.configuration_metadata(record)["status"] == "unavailable"


def test_saved_config_symlink_cannot_escape_identity_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (root / "external").symlink_to(other, target_is_directory=True)
    with pytest.raises(PodgroveError, match="outside the worktree"):
        state.configuration_root({"root": str(root), "config_root": str(root / "external")})


def test_legacy_subdirectory_record_blocks_adoption_or_second_environment(project, capsys):
    root, kube, created = project
    previous = root / "backend"
    path = state.state_path(previous, "offline-identity")
    record = {"identity": state.identity(previous), "root": str(previous), "context": "offline-identity",
              "namespace": "developer", "namespace_mode": "shared", "status": "ready", "podgrove_version": "0.2.0"}
    state.write(path, record)
    for selected in (root, previous, previous / "src"):
        with pytest.raises(PodgroveError, match="Legacy subdirectory.*0.2.0"):
            invoke("up", selected)
    assert state.read(path) == record and created == []
    assert not state.state_path(root, "offline-identity").exists()
    kube.create_environment.assert_not_called()
    kube.destroy.assert_not_called()


def test_nested_independent_checkout_is_not_misidentified_as_legacy_state(project, capsys):
    root, _, _ = project
    nested = checkout(root / "independent")
    record = {"identity": state.identity(nested), "root": str(nested), "context": "offline-identity",
              "namespace": "developer", "namespace_mode": "shared", "status": "ready"}
    state.write(state.state_path(nested, "offline-identity"), record)
    assert invoke("up", root, "--json") == 0
    assert json.loads(capsys.readouterr().out)["identity"] == state.identity(root)
