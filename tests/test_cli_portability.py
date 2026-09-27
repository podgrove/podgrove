"""Public config-to-bootstrap/workload target agreement without a live cluster."""
from __future__ import annotations

import json
import sys
from unittest.mock import Mock

import pytest
import yaml

from podgrove import cli, state, web
from podgrove.bootstrap import PROVISIONING_MARKER
from podgrove.kube import ENVIRONMENT, Kube, resolve_namespace


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    root = (tmp_path / "application").resolve()
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.delenv("PODGROVE_CONTEXT", raising=False)
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "private-state"))
    (root / "compose.yml").write_text("services:\n  api:\n    image: busybox:1.37\n")
    model = {"services": {"api": {"image": "busybox:1.37"}}}
    compose = Mock()
    compose.model.return_value = model
    compose.published_ports.return_value = []
    monkeypatch.setattr(cli, "Compose", Mock(return_value=compose))
    monkeypatch.setattr(cli, "Kube", Mock(side_effect=AssertionError("No live cluster access")))
    return root


def configure(root, namespace="team-dev", mode="shared", **extra):
    cluster = {"context": "chosen-cluster", "namespace_mode": mode, "storage_class": "fast.csi", **extra}
    if namespace is not None:
        cluster["namespace"] = namespace
    (root / "podgrove.yml").write_text(yaml.safe_dump({"cluster": cluster}))


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["podgrove", *map(str, args)])
    return cli.main()


def objects(folder):
    return [obj for path in sorted(folder.glob("*.yaml")) for obj in yaml.safe_load_all(path.read_text())]


@pytest.mark.parametrize("base", ["team-dev", "wt-shared-name", "a" * 63, "7"])
@pytest.mark.parametrize("mode", ["shared", "worktree"])
def test_bootstrap_and_workloads_resolve_identical_namespace_and_storage(worktree, tmp_path, monkeypatch, capsys, base, mode):
    configure(worktree, base, mode)
    output = tmp_path / "platform"
    assert invoke(monkeypatch, "bootstrap", "--output", output) == 0
    printed = capsys.readouterr().out
    expected = resolve_namespace(base, mode, state.identity(worktree))
    assert f"namespace {expected} ({mode})" in printed
    assert f"kubectl --context chosen-cluster --namespace {expected} apply -f" in printed
    bundle = objects(output)
    marker = next(obj for obj in bundle if obj["kind"] == "ConfigMap" and
                  obj["metadata"]["name"] == PROVISIONING_MARKER)
    assert marker["metadata"]["namespace"] == expected
    assert marker["data"]["namespace_mode"] == mode
    assert marker["data"].get("environment") == (state.identity(worktree) if mode == "worktree" else None)
    assert all(obj["metadata"]["namespace"] == expected for obj in bundle)
    assert all(ENVIRONMENT not in obj["metadata"].get("labels", {}) for obj in bundle)
    assert {obj["kind"] for obj in bundle} <= {"ConfigMap", "ServiceAccount", "Role", "RoleBinding", "NetworkPolicy"}
    assert "fast.csi" not in yaml.safe_dump(bundle)
    assert invoke(monkeypatch, "up", "--dry-run", "--json") == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["namespace"] == expected and rendered["namespace_mode"] == mode
    assert all(obj["metadata"]["namespace"] == expected for obj in rendered["resources"])
    claim = next(obj for obj in rendered["resources"] if obj["kind"] == "PersistentVolumeClaim")
    assert claim["spec"]["storageClassName"] == "fast.csi"
    assert not (tmp_path / "private-state").exists()


@pytest.mark.parametrize("mode", ["shared", "worktree"])
def test_two_worktrees_share_or_separate_bootstrap_with_unique_engines(worktree, tmp_path, monkeypatch, capsys, mode):
    sibling = (tmp_path / "second-worktree").resolve()
    sibling.mkdir()
    (sibling / "compose.yml").write_text((worktree / "compose.yml").read_text())
    namespaces, object_identities, engines = [], [], []
    for index, root in enumerate((worktree, sibling)):
        configure(root, "team-base", mode)
        folder = tmp_path / f"platform-{index}"
        assert invoke(monkeypatch, "bootstrap", "--project-directory", root, "--output", folder) == 0
        capsys.readouterr()
        bundle = objects(folder)
        namespaces.append(next(obj["metadata"]["namespace"] for obj in bundle if obj["kind"] == "ConfigMap"))
        object_identities.append({(obj["kind"], obj["metadata"]["namespace"], obj["metadata"]["name"])
                                  for obj in bundle})
        assert invoke(monkeypatch, "up", "--project-directory", root, "--dry-run", "--json") == 0
        rendered = json.loads(capsys.readouterr().out)
        engines.append(next(obj["metadata"]["name"] for obj in rendered["resources"] if obj["kind"] == "StatefulSet"))
        assert rendered["namespace"] == namespaces[-1]
    assert engines[0] != engines[1]
    if mode == "shared":
        assert namespaces[0] == namespaces[1] and object_identities[0] == object_identities[1]
    else:
        assert namespaces[0] != namespaces[1] and object_identities[0].isdisjoint(object_identities[1])


@pytest.mark.parametrize("command", [
    ["bootstrap", "--output", "generated"], ["validate"], ["up"], ["up", "--dry-run"], ["doctor"],
    ["web"], ["status"], ["status", "--all"], ["logs", "api"], ["exec", "api", "--", "true"],
    ["down"], ["reap", "--dry-run"],
])
def test_missing_namespace_stops_before_compose_cluster_browser_or_state(worktree, tmp_path, monkeypatch, capsys, command):
    configure(worktree, namespace=None)
    forbidden = Mock(side_effect=AssertionError("Missing namespace must stop before side effects"))
    monkeypatch.setattr(cli, "Compose", forbidden)
    monkeypatch.setattr(web, "serve", forbidden)
    assert invoke(monkeypatch, *command) == 1
    assert "No namespace provided" in capsys.readouterr().err
    forbidden.assert_not_called()
    assert not (tmp_path / "private-state").exists()
    assert not (worktree / "generated").exists()


def test_worktree_dashboard_and_inventory_use_same_resolved_namespace(worktree, monkeypatch):
    configure(worktree, "team-base", "worktree")
    selected = resolve_namespace("team-base", "worktree", state.identity(worktree))
    serve = Mock(return_value=0)
    records = Mock(return_value=[])
    monkeypatch.setattr(web, "serve", serve)
    monkeypatch.setattr(state, "local_records", records)
    assert invoke(monkeypatch, "web", "--no-open") == 0
    serve.assert_called_once_with("chosen-cluster", port=0, namespace=selected, open_browser=False)
    assert invoke(monkeypatch, "status", "--all", "--json") == 0
    records.assert_called_once_with("chosen-cluster", selected)


def test_exact_namespace_flag_does_not_derive_worktree_namespace_twice(worktree, monkeypatch):
    configure(worktree, "team-base", "worktree")
    selected = resolve_namespace("team-base", "worktree", state.identity(worktree))
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    assert invoke(monkeypatch, "web", "--namespace", selected) == 0
    assert serve.call_args.kwargs["namespace"] == selected


def test_bootstrap_without_storage_creates_only_namespaced_setup(worktree, tmp_path, monkeypatch, capsys):
    (worktree / "podgrove.yml").write_text("cluster: {context: chosen-cluster, namespace: team-dev}\n")
    assert invoke(monkeypatch, "bootstrap", "--output", tmp_path / "platform") == 0
    assert not capsys.readouterr().err
    bundle = objects(tmp_path / "platform")
    assert len(bundle) == 9
    assert all(obj["metadata"]["namespace"] == "team-dev" for obj in bundle)


@pytest.mark.parametrize("name", ["", "../class", "Bad.Class", "part..part", "x" * 64])
def test_bad_storage_override_is_not_silently_replaced_by_default(worktree, monkeypatch, capsys, name):
    configure(worktree)
    assert invoke(monkeypatch, "up", "--storage-class", name, "--dry-run") == 1
    assert "storage_class" in capsys.readouterr().err


def test_bootstrap_explicit_overrides_target_and_storage(worktree, tmp_path, monkeypatch, capsys):
    configure(worktree, "old-base", "worktree")
    folder = tmp_path / "platform"
    assert invoke(monkeypatch, "bootstrap", "--context", "new-cluster", "--namespace", "new-team",
                  "--storage-class", "new-disk", "--output", folder) == 0
    text = capsys.readouterr().out
    assert "context new-cluster, namespace new-team (shared)" in text
    assert "new-disk" not in "".join(path.read_text() for path in folder.glob("*.yaml"))
    assert all(obj["metadata"]["namespace"] == "new-team" for obj in objects(folder))
    assert "old-base" not in "".join(path.read_text() for path in folder.glob("*.yaml"))


@pytest.mark.parametrize("command", [["status", "--all", "--json"], ["web", "--no-open"]])
def test_explicit_shared_scope_does_not_need_a_usable_cwd(monkeypatch, command):
    def no_cwd():
        raise FileNotFoundError("cwd was removed")

    monkeypatch.setattr(cli.os, "getcwd", no_cwd)
    monkeypatch.setattr(state, "identity", Mock(side_effect=AssertionError("Shared scope needs no worktree identity")))
    records = Mock(return_value=[])
    serve = Mock(return_value=0)
    monkeypatch.setattr(state, "local_records", records)
    monkeypatch.setattr(web, "serve", serve)
    assert invoke(monkeypatch, *command, "--context", "chosen-cluster", "--namespace", "team-dev") == 0
    if command[0] == "status":
        records.assert_called_once_with("chosen-cluster", "team-dev")
    else:
        serve.assert_called_once_with("chosen-cluster", port=0, namespace="team-dev", open_browser=False)


@pytest.mark.parametrize("mode, owner", [("shared", "self"), ("worktree", "other"), ("worktree", None)])
def test_doctor_refuses_bootstrap_marker_ownership_conflicts_before_admission(worktree, monkeypatch, capsys, mode, owner):
    from podgrove.bootstrap import provisioning_marker
    configure(worktree, "team-base", mode)
    ident = state.identity(worktree)
    target = resolve_namespace("team-base", mode, ident)
    kube = Kube("chosen-cluster", target, namespace_mode=mode)
    marker = provisioning_marker(target, mode, ident if mode == "worktree" else None)
    if owner:
        marker["data"]["environment"] = ident if owner == "self" else "aaaaaaaaaaaa"
    else:
        marker["data"].pop("environment", None)
    kube.get = Mock(return_value=marker)
    kube.preflight = Mock()
    kube.call = Mock(side_effect=AssertionError("No Kubernetes mutation or admission after ownership conflict"))
    kube.check_admission = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    assert invoke(monkeypatch, "doctor") == 1
    assert "marker identity" in capsys.readouterr().err
    kube.get.assert_called_once_with("configmap", "podgrove-bootstrap")
    kube.check_admission.assert_not_called()
    kube.call.assert_not_called()
