"""Offline rendering, portability and filesystem boundaries."""
from pathlib import Path
import os

import pytest
import yaml

from podgrove.bootstrap import ENVIRONMENT, bootstrap_names, generate_bootstrap, render_bootstrap
from podgrove.errors import PodgroveError


def flatten(files):
    return [item for items in files.values() for item in items]


@pytest.mark.parametrize("namespace", ["default", "team-a", "team-a-012345abcdef", "n" * 63])
def test_portable_namespace_and_group_references_are_exclusively_namespaced(namespace):
    group = "engineering:podgrove:team-a"
    rendered = render_bootstrap(namespace, developer_group=group)
    docs = flatten(rendered)
    assert len(rendered) == 5 and len(docs) == 9
    assert {item["kind"] for item in docs} == {"ConfigMap", "ServiceAccount", "Role", "RoleBinding", "NetworkPolicy"}
    for item in docs:
        assert item["metadata"]["namespace"] == namespace
        assert ENVIRONMENT not in item["metadata"].get("labels", {})
        for subject in item.get("subjects", []):
            if subject["kind"] == "ServiceAccount":
                assert subject["namespace"] == namespace
            else:
                assert subject == {"kind": "Group", "name": group, "apiGroup": "rbac.authorization.k8s.io"}
        if "roleRef" in item:
            assert item["roleRef"]["kind"] == "Role"
        for rule in item.get("rules", []):
            assert not set(rule["resources"]) & {"namespaces", "nodes", "storageclasses", "persistentvolumes",
                                                  "clusterroles", "clusterrolebindings"}
    serialized = yaml.safe_dump_all(docs)
    assert "podgrove-testing" not in serialized and "gp3" not in serialized


def test_two_installations_have_disjoint_namespaced_resources_and_no_cluster_objects():
    def identities(namespace):
        return {(d["kind"], d["metadata"]["namespace"], d["metadata"]["name"])
                for d in flatten(render_bootstrap(namespace))}
    assert identities("team-a").isdisjoint(identities("team-b"))
    assert bootstrap_names("team-a") == bootstrap_names("team-b")


def test_worktree_identity_is_only_in_retained_provisioning_marker_data():
    docs = flatten(render_bootstrap("team-012345abcdef", namespace_mode="worktree", identity="012345abcdef"))
    marker, = [d for d in docs if d["kind"] == "ConfigMap"]
    assert marker["metadata"]["name"] == "podgrove-bootstrap"
    assert marker["data"] == {"version": "1", "namespace_mode": "worktree", "environment": "012345abcdef"}
    assert all(ENVIRONMENT not in d["metadata"].get("labels", {}) for d in docs)


@pytest.mark.parametrize("kwargs", [
    {"namespace": "kube-system"}, {"namespace": "kube-public"},
    {"namespace": "A"}, {"namespace": "a.b"}, {"namespace": "a" * 64}, {"namespace": ""},
    {"storage_class": ""}, {"storage_class": ".."},
    {"storage_class": "a" * 254}, {"storage_class": "a" * 64}, {"storage_class": "Uppercase"}, {"storage_class": "-bad"},
    {"storage_class": "bad/class"}, {"developer_group": ""}, {"developer_group": "group\nadmin"},
    {"developer_group": "x\x7f"}, {"namespace_mode": "exclusive"},
    {"namespace_mode": "worktree"}, {"namespace_mode": "worktree", "identity": "OTHER"},
    {"identity": "012345abcdef"}, {"include_tainted_nodes": "false"}, {"include_tainted_nodes": True},
])
def test_invalid_inputs_create_nothing(tmp_path, kwargs):
    target = tmp_path / "install"
    with pytest.raises(PodgroveError):
        generate_bootstrap(target, **{"namespace": "team-dev", "storage_class": "delete-sc", **kwargs})
    assert not target.exists()


def test_generated_directory_contains_only_standalone_yaml_and_no_side_effects(tmp_path, monkeypatch):
    import subprocess
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Bootstrap must stay offline")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    target = tmp_path / "install"
    paths = generate_bootstrap(target, namespace="team-dev", storage_class="delete-sc")
    assert len(paths) == 5 and sorted(target.iterdir()) == paths
    expected = render_bootstrap("team-dev", "delete-sc")
    for path in paths:
        assert path.is_file() and not path.is_symlink() and path.suffix == ".yaml"
        assert list(yaml.safe_load_all(path.read_text())) == expected[path.name]
    assert sorted(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("kind", ["empty", "populated", "file", "symlink", "parent-symlink", "missing-parent"])
def test_existing_or_unsafe_output_is_never_modified(tmp_path, kind):
    actual = tmp_path / "actual"
    actual.mkdir()
    sentinel = actual / "untouched"
    sentinel.write_text("unrelated")
    target = tmp_path / "install"
    if kind in {"empty", "populated"}:
        target.mkdir()
        if kind == "populated":
            (target / "custom.yaml").write_text("unrelated")
    elif kind == "file":
        target.write_text("unrelated")
    elif kind == "symlink":
        target.symlink_to(actual, target_is_directory=True)
    elif kind == "parent-symlink":
        link = tmp_path / "parent"
        link.symlink_to(actual, target_is_directory=True)
        target = link / "install"
    else:
        target = tmp_path / "absent" / "install"
    with pytest.raises(PodgroveError, match="new output directory"):
        generate_bootstrap(target, namespace="team-dev", storage_class="delete-sc")
    assert sentinel.read_text() == "unrelated" and list(actual.iterdir()) == [sentinel]
    if kind == "populated":
        assert (target / "custom.yaml").read_text() == "unrelated"
    if kind == "file":
        assert target.read_text() == "unrelated"
    if kind in {"parent-symlink", "missing-parent"}:
        assert not target.exists()


def test_failed_write_removes_only_files_created_by_this_generation(tmp_path, monkeypatch):
    original = os.open
    target = tmp_path / "install"
    def failing_open(path, *args, **kwargs):
        if path == "30-developer-bindings.yaml":
            raise OSError("simulated disk full")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", failing_open)
    with pytest.raises(PodgroveError, match="simulated disk full"):
        generate_bootstrap(target, namespace="team-dev", storage_class="delete-sc")
    assert not target.exists()


def test_failed_write_preserves_unrelated_file_added_to_output(tmp_path, monkeypatch):
    original = os.open
    target = tmp_path / "install"
    def failing_open(path, *args, **kwargs):
        if path == "30-developer-bindings.yaml":
            (target / "unrelated.txt").write_text("keep this")
            raise OSError("simulated disk full")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", failing_open)
    with pytest.raises(PodgroveError, match="simulated disk full"):
        generate_bootstrap(target, namespace="team-dev", storage_class="delete-sc")
    assert list(target.iterdir()) == [target / "unrelated.txt"]
    assert (target / "unrelated.txt").read_text() == "keep this"


def test_replaced_output_directory_is_refused_before_any_manifest_write(tmp_path, monkeypatch):
    original = os.open
    target, displaced = tmp_path / "install", tmp_path / "original-directory"
    def replacing_open(path, flags, *args, **kwargs):
        if path == "install" and flags & os.O_DIRECTORY:
            target.rename(displaced)
            target.mkdir()
            (target / "user-data").write_text("foreign replacement")
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", replacing_open)
    with pytest.raises(PodgroveError, match="directory changed before writing"):
        generate_bootstrap(target, namespace="team-dev", storage_class="delete-sc")
    assert list(displaced.iterdir()) == []
    assert list(target.iterdir()) == [target / "user-data"]
    assert (target / "user-data").read_text() == "foreign replacement"


def test_directory_replacement_mid_write_never_redirects_writes_or_cleanup(tmp_path, monkeypatch):
    original = os.open
    target, displaced = tmp_path / "install", tmp_path / "original-directory"
    def replacing_open(path, flags, *args, **kwargs):
        if path == "10-client-rbac.yaml" and flags & os.O_CREAT:
            target.rename(displaced)
            target.mkdir()
            (target / "user-data").write_text("foreign replacement")
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", replacing_open)
    with pytest.raises(PodgroveError, match="directory changed while writing"):
        generate_bootstrap(target, namespace="team-dev", storage_class="delete-sc")
    assert sorted(p.name for p in displaced.iterdir()) == ["00-provisioning-marker.yaml", "05-network-isolation.yaml", "10-client-rbac.yaml"]
    assert list(target.iterdir()) == [target / "user-data"]
    assert (target / "user-data").read_text() == "foreign replacement"


@pytest.mark.parametrize("replacement", [True, False], ids=["new-inode", "edited-in-place"])
def test_rollback_preserves_replaced_or_edited_generated_file(tmp_path, monkeypatch, replacement):
    original = os.open
    target = tmp_path / "install"
    first = target / "00-provisioning-marker.yaml"
    def failing_open(path, *args, **kwargs):
        if path == "30-developer-bindings.yaml":
            old_inode = first.stat().st_ino
            if replacement:
                other = target / "user-replacement.tmp"
                other.write_text("user-owned replacement")
                os.replace(other, first)
                assert first.stat().st_ino != old_inode
            else:
                first.write_text("user-owned replacement")
                assert first.stat().st_ino == old_inode
            raise OSError("simulated disk full")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", failing_open)
    with pytest.raises(PodgroveError, match="Partial files may remain"):
        generate_bootstrap(target, namespace="team-dev", storage_class="delete-sc")
    assert list(target.iterdir()) == [first]
    assert first.read_text() == "user-owned replacement"


def test_concurrently_added_manifest_refuses_success_and_preserves_user_file(tmp_path, monkeypatch):
    original = os.listdir
    target = tmp_path / "install"
    def injecting_listdir(path):
        (target / "user-manifest.yaml").write_text("kind: DoNotTouch\n")
        return original(path)
    monkeypatch.setattr(os, "listdir", injecting_listdir)
    with pytest.raises(PodgroveError, match="files changed while writing"):
        generate_bootstrap(target, namespace="team-dev", storage_class="delete-sc")
    assert list(target.iterdir()) == [target / "user-manifest.yaml"]
    assert (target / "user-manifest.yaml").read_text() == "kind: DoNotTouch\n"


def test_legacy_storage_argument_never_emits_storage_grants_or_changes():
    assert render_bootstrap("team-dev", "old-delete-sc") == render_bootstrap("team-dev")


def test_separate_renders_do_not_share_mutable_safeguards():
    first = render_bootstrap("team-a")
    first["05-network-isolation.yaml"][0]["spec"]["egress"].append({})
    first["00-provisioning-marker.yaml"][0]["data"]["namespace_mode"] = "invalid"
    second = render_bootstrap("team-b")
    assert second["05-network-isolation.yaml"][0]["spec"]["egress"] == []
    assert second["00-provisioning-marker.yaml"][0]["data"] == {"version": "1", "namespace_mode": "shared"}


def test_deploy_tree_contains_no_apply_ready_hardcoded_namespace_bundle():
    deploy = Path(__file__).resolve().parents[1] / "deploy"
    assert not list(deploy.rglob("*.yaml"))
    templates = list((deploy / "reaper").glob("*.yaml.example"))
    assert len(templates) == 3
    for template in templates:
        assert "podgrove-testing" not in template.read_text()
        assert "PODGROVE_NAMESPACE" in template.read_text()


@pytest.mark.parametrize("missing", ["context", "namespace"])
def test_cli_missing_required_bootstrap_target_fails_before_output_or_external_work(tmp_path, monkeypatch, missing):
    from podgrove import cli
    cluster = {"context": "offline-context", "namespace": "team-dev", "storage_class": "delete-sc"}
    cluster.pop(missing)
    (tmp_path / "podgrove.yml").write_text(yaml.safe_dump({"cluster": cluster}))
    monkeypatch.delenv("PODGROVE_CONTEXT", raising=False)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Bootstrap may not contact a cluster, normalize Compose or create session state")
    monkeypatch.setattr(cli, "Kube", forbidden)
    monkeypatch.setattr(cli, "Compose", forbidden)
    monkeypatch.setattr(cli.state, "state_path", forbidden)
    target = tmp_path / "install"
    args = cli.parser().parse_args(["bootstrap", "--project-directory", str(tmp_path), "--output", str(target)])
    with pytest.raises(PodgroveError, match="context|namespace|storage_class"):
        cli.execute(args)
    assert not target.exists()


def test_cli_bootstrap_uses_explicit_overrides_without_opening_compose_or_starting_sessions(tmp_path, monkeypatch, capsys):
    from podgrove import cli
    (tmp_path / "podgrove.yml").write_text(yaml.safe_dump({
        "cluster": {"context": "configured-context", "namespace": "configured-team", "storage_class": "configured-sc"},
        "compose": {"files": ["not-present.yaml"], "env_file": "not-present.env"},
    }))
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Bootstrap must not start external or runtime work")
    monkeypatch.setattr(cli, "Kube", forbidden)
    monkeypatch.setattr(cli, "Compose", forbidden)
    monkeypatch.setattr(cli.state, "state_path", forbidden)
    target = tmp_path / "install"
    args = cli.parser().parse_args(["bootstrap", "--project-directory", str(tmp_path), "--output", str(target),
                                   "--context", "override-context", "--namespace", "override-team",
                                   "--storage-class", "override-sc", "--developer-group", "override-group"])
    assert cli.execute(args) == 0
    expected = render_bootstrap("override-team", "override-sc", "override-group")
    assert {path.name: list(yaml.safe_load_all(path.read_text())) for path in target.iterdir()} == expected
    output = capsys.readouterr().out
    assert "kubectl --context override-context --namespace override-team apply -f" in output
    assert "configured-context" not in output
