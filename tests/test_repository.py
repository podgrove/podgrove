"""Synthetic metadata only: no Git commands, repositories or network access."""
from copy import deepcopy
import os
import subprocess

import pytest

from podgrove import repository
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube, manifests

IDENT = "123456abcdef"


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Repository labels must never invoke a command")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.delenv("PODGROVE_REPO", raising=False)
    monkeypatch.delenv("PODGROVE_BRANCH", raising=False)


def ordinary(tmp_path, head="ref: refs/heads/feature/namespace-only\n"):
    root = tmp_path / "example-repository"
    metadata = root / ".git"
    metadata.mkdir(parents=True)
    (metadata / "HEAD").write_text(head)
    (metadata / "config").write_text("must never read remote URL or credential config\n")
    return root, metadata


def linked(tmp_path, *, relative=False, bare=False):
    root = tmp_path / "worktree-checkout"
    root.mkdir()
    common = tmp_path / "actual-repository.git" if bare else tmp_path / "actual-repository" / ".git"
    gitdir = common / "worktrees" / "checkout-internal-name"
    gitdir.mkdir(parents=True)
    (root / ".git").write_text("gitdir: " + (os.path.relpath(gitdir, root) if relative else str(gitdir)) + "\n")
    (gitdir / "commondir").write_text("../..\n")
    (gitdir / "HEAD").write_text("ref: refs/heads/my/worktree\n")
    (common / "HEAD").write_text("ref: refs/heads/main\n")
    return root, gitdir, common


def test_ordinary_checkout_reads_only_head_and_returns_repo_branch_without_mutation(tmp_path, monkeypatch):
    root, metadata = ordinary(tmp_path)
    before = {path: path.read_bytes() for path in metadata.iterdir()}
    original, paths = repository._read, []
    def read(path):
        paths.append(path)
        assert path.name in ("HEAD", "commondir")
        return original(path)
    monkeypatch.setattr(repository, "_read", read)
    assert repository.repository_labels(root) == {"repo": "example-repository", "branch": "feature/namespace-only"}
    assert {path: path.read_bytes() for path in metadata.iterdir()} == before
    assert metadata / "HEAD" in paths


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("bare", [False, True])
def test_linked_worktree_uses_common_repository_name_but_own_head(tmp_path, relative, bare):
    root, gitdir, common = linked(tmp_path, relative=relative, bare=bare)
    before = {path: path.read_bytes() for path in (root / ".git", gitdir / "commondir", gitdir / "HEAD", common / "HEAD")}
    assert repository.repository_labels(root) == {"repo": "actual-repository", "branch": "my/worktree"}
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("digest", ["ABCDEF012345" + "0" * 28, "0123456789ab" + "f" * 52])
def test_detached_head_has_explicit_short_sha_label(tmp_path, digest):
    root, _ = ordinary(tmp_path, digest + "\n")
    assert repository.repository_labels(root)["branch"] == "detached-" + digest[:12].lower()


def test_environment_overrides_skip_metadata_if_both_are_explicit(tmp_path, monkeypatch):
    root, _ = ordinary(tmp_path)
    monkeypatch.setattr(repository, "_metadata", lambda *_: pytest.fail("No metadata reads when both overrides are set"))
    assert repository.repository_labels(root, {"PODGROVE_REPO": "team-repo", "PODGROVE_BRANCH": ""}) == {
        "repo": "team-repo", "branch": ""}


@pytest.mark.parametrize("field,variable", [("repo", "PODGROVE_REPO"), ("branch", "PODGROVE_BRANCH")])
def test_one_override_keeps_other_inferred_field(tmp_path, field, variable):
    root, _, _ = linked(tmp_path)
    expected = {"repo": "actual-repository", "branch": "my/worktree", field: "explicit"}
    assert repository.repository_labels(root, {variable: "explicit", "GIT_DIR": "/unrelated/metadata"}) == expected


@pytest.mark.parametrize("head", ["", "0123456", "not metadata", "ref: refs/tags/release", "ref: refs/heads/",
    "ref: refs/heads/a..b", "ref: refs/heads/a.lock", "ref: refs/heads/a//b", "ref: refs/heads/.hidden",
    "ref: refs/heads/a@{b", "ref: refs/heads/x?y", "ref: refs/heads/x\\y", "ref: refs/heads/x[y",
    "ref: refs/heads/x~y", "ref: refs/heads/x y", "ref: refs/heads/x\ny", "ref: refs/heads/x\x00y"])
def test_unrecognized_head_falls_back_without_echoing_metadata(tmp_path, head):
    root, _ = ordinary(tmp_path, head)
    assert repository.repository_labels(root)["branch"] == "unspecified"


@pytest.mark.parametrize("case", ["missing", "malformed-pointer", "missing-target", "invalid-utf8", "oversized",
                                  "head-symlink", "gitdir-symlink", "fifo", "missing-common"])
def test_unusable_metadata_is_bounded_and_falls_back(tmp_path, case):
    root, metadata = ordinary(tmp_path)
    if case in ("missing", "malformed-pointer", "missing-target", "gitdir-symlink"):
        moved = root / "metadata"
        metadata.rename(moved)
        if case == "malformed-pointer":
            metadata.write_text("unexpected pointer\n")
        elif case == "missing-target":
            metadata.write_text("gitdir: /definitely-missing-metadata\n")
        elif case == "gitdir-symlink":
            metadata.symlink_to(moved, target_is_directory=True)
    elif case == "invalid-utf8":
        (metadata / "HEAD").write_bytes(b"\xff\x00")
    elif case == "oversized":
        (metadata / "HEAD").write_text("ref: refs/heads/" + "a" * 5000)
    elif case == "head-symlink":
        (metadata / "HEAD").unlink()
        (metadata / "HEAD").symlink_to(metadata / "config")
    elif case == "fifo":
        (metadata / "HEAD").unlink()
        os.mkfifo(metadata / "HEAD")
    else:
        (metadata / "commondir").write_text("/definitely-missing-common-metadata\n")
    assert repository.repository_labels(root) == {"repo": "example-repository", "branch": "unspecified"}


def test_no_metadata_uses_directory_fallback(tmp_path):
    assert repository.repository_labels(tmp_path / "plain-source") == {"repo": "plain-source", "branch": "unspecified"}


def test_manifests_use_bounded_kubernetes_labels_and_do_not_store_paths_or_urls(tmp_path):
    root, _, _ = linked(tmp_path)
    resources = manifests("chosen-namespace", IDENT, root, "small", 600)
    for resource in resources:
        labels = resource["metadata"]["labels"]
        assert labels["podgrove.dev/repo"] == "actual-repository"
        assert labels["podgrove.dev/branch"] == "my-worktree"
        assert all(len(value) <= 63 and "://" not in value and str(tmp_path) not in value for value in labels.values())


def test_checkout_branch_and_repo_changes_do_not_force_existing_controller_recreation(tmp_path):
    root, metadata = ordinary(tmp_path, "ref: refs/heads/before\n")
    old = next(item for item in manifests("chosen-namespace", IDENT, root, "small", 600) if item["kind"] == "StatefulSet")
    before = deepcopy(old)
    (metadata / "HEAD").write_text("ref: refs/heads/after\n")
    new = next(item for item in manifests("chosen-namespace", IDENT, root, "small", 600) if item["kind"] == "StatefulSet")
    new["spec"]["template"]["metadata"]["labels"]["podgrove.dev/repo"] = "renamed-checkout"
    Kube("chosen", "chosen-namespace")._validate_existing(new, old, IDENT)
    assert old == before
    assert old["spec"]["template"]["metadata"]["labels"]["podgrove.dev/branch"] == "before"


@pytest.mark.parametrize("key", [MANAGED, ENVIRONMENT, "podgrove.dev/worktree", "podgrove.dev/node-mode", "podgrove.dev/owner"])
def test_advisory_label_exception_never_ignores_ownership_or_scheduling_labels(tmp_path, key):
    root, _ = ordinary(tmp_path)
    new = next(item for item in manifests("chosen-namespace", IDENT, root, "small", 600) if item["kind"] == "StatefulSet")
    old = deepcopy(new)
    old["spec"]["template"]["metadata"]["labels"][key] = "foreign"
    with pytest.raises(PodgroveError, match="different engine settings"):
        Kube("chosen", "chosen-namespace")._validate_existing(new, old, IDENT)
