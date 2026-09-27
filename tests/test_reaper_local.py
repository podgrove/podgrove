import copy
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from podgrove import reaper, state
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube


def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "worktree"
    root.mkdir()
    ident = state.identity(root)
    data = {"root": str(root), "context": "test-cluster", "namespace": "default", "identity": ident,
            "last_activity": time.time(), "ttl_seconds": 3600}
    path = state.state_path(root, data["context"])
    state.write(path, data)
    lease = {"metadata": {"name": f"pg-{ident}", "resourceVersion": "1", "namespace": "default",
                          "labels": {MANAGED: "podgrove", ENVIRONMENT: ident}},
             "data": {"last_activity": str(data["last_activity"]), "ttl_seconds": "3600"}}
    kube = Kube("test-cluster", "default")
    kube.get = Mock(side_effect=[{"items": [lease]}, lease])
    kube.destroy = Mock()
    stop = Mock()
    monkeypatch.setattr(reaper, "_stop_local", stop)
    return root, path, data, lease, kube, stop


def test_missing_local_worktree_reaped_and_local_state_removed(tmp_path, monkeypatch):
    root, path, data, _, kube, stop = environment(tmp_path, monkeypatch)
    root.rmdir()
    path.with_suffix(".log").write_text("session log")
    assert reaper.reap(kube) == [{"identity": data["identity"],
                                "reason": "local worktree no longer exists", "deleted": True}]
    stop.assert_called_once()
    kube.destroy.assert_called_once_with(data["identity"], namespace_mode="shared")
    assert not path.exists()
    assert not path.with_suffix(".log").exists()
    assert not path.with_suffix(".lock").exists()


def test_missing_worktree_dry_run_keeps_state_and_resources(tmp_path, monkeypatch):
    root, path, _, _, kube, stop = environment(tmp_path, monkeypatch)
    root.rmdir()
    assert reaper.reap(kube, dry_run=True)[0]["deleted"] is False
    assert path.exists()
    stop.assert_not_called()
    kube.destroy.assert_not_called()


@pytest.mark.parametrize("change", ["foreign-context", "foreign-namespace", "no-local-record"])
def test_cluster_lease_alone_does_not_prove_missing_local_worktree(tmp_path, monkeypatch, change):
    root, path, data, _, kube, stop = environment(tmp_path, monkeypatch)
    root.rmdir()
    path.unlink()
    if change != "no-local-record":
        data["context" if change == "foreign-context" else "namespace"] = "another"
        state.write(state.state_path(root, data["context"]), data)
    assert reaper.reap(kube) == []
    kube.destroy.assert_not_called()
    stop.assert_not_called()


def test_cleanup_failure_preserves_local_retry_record(tmp_path, monkeypatch):
    root, path, _, _, kube, stop = environment(tmp_path, monkeypatch)
    root.rmdir()
    kube.destroy.side_effect = PodgroveError("PVC deletion pending")
    result = reaper.reap(kube)
    assert result[0]["deleted"] is False
    assert "PVC deletion pending" in result[0]["error"]
    assert path.exists()
    stop.assert_called_once()


def test_recreated_worktree_before_delete_is_retained(tmp_path, monkeypatch):
    root, _, _, lease, kube, stop = environment(tmp_path, monkeypatch)
    root.rmdir()
    def get(*args, **kwargs):
        if len(args) == 1:
            return {"items": [lease]}
        root.mkdir()
        return lease
    kube.get.side_effect = get
    assert reaper.reap(kube) == []
    kube.destroy.assert_not_called()
    stop.assert_not_called()


def test_replaced_symlink_is_not_missing_worktree_proof(tmp_path, monkeypatch):
    root, _, _, _, kube, stop = environment(tmp_path, monkeypatch)
    root.rmdir()
    root.symlink_to(tmp_path / "missing-target")
    # Binding validation skips a state whose resolved root changed.
    assert reaper.reap(kube) == []
    kube.destroy.assert_not_called()
    stop.assert_not_called()


def test_missing_parent_is_not_proof_of_deleted_worktree(tmp_path):
    record = {"data": {"root": str(tmp_path / "missing-parent" / "worktree")}}
    assert reaper._missing_worktree(record) is False


def test_failed_path_stat_refuses_orphan_cleanup(monkeypatch):
    monkeypatch.setattr(Path, "lstat", Mock(side_effect=PermissionError("not accessible")))
    with pytest.raises(PodgroveError, match="refusing orphan cleanup"):
        reaper._missing_worktree({"data": {"root": "/some/worktree"}})


def test_ttl_cleanup_removes_matching_local_state_with_existing_worktree(tmp_path, monkeypatch):
    root, path, _, lease, kube, stop = environment(tmp_path, monkeypatch)
    expired = copy.deepcopy(lease)
    expired["data"]["last_activity"] = "100"
    kube.get.side_effect = [{"items": [expired]}, expired]
    assert reaper.reap(kube)[0]["reason"] == "idle TTL expired"
    assert root.is_dir()
    assert not path.exists()
    stop.assert_called_once()


def test_failed_authenticated_stop_keeps_environment_and_retry_state(tmp_path, monkeypatch):
    root, path, _, _, kube, stop = environment(tmp_path, monkeypatch)
    root.rmdir()
    stop.side_effect = PodgroveError("Session is still stopping")
    assert reaper.reap(kube)[0]["deleted"] is False
    kube.destroy.assert_not_called()
    assert path.exists()


def test_targeted_reap_does_not_touch_another_environment(tmp_path, monkeypatch):
    root, _, data, lease, kube, _ = environment(tmp_path, monkeypatch)
    root.rmdir()
    other = copy.deepcopy(lease)
    other["metadata"].update(name="pg-123456abcdef", labels={MANAGED: "podgrove", ENVIRONMENT: "123456abcdef"})
    other["data"]["last_activity"] = "100"
    kube.get.side_effect = [{"items": [other, lease]}, lease]
    assert reaper.reap(kube, identity=data["identity"])[0]["identity"] == data["identity"]
    kube.get.assert_any_call("configmap", selector=f"{MANAGED}=podgrove,{ENVIRONMENT}={data['identity']}")
    kube.destroy.assert_called_once_with(data["identity"], namespace_mode="shared")


@pytest.mark.parametrize("identity", ["", "../default", "123", "ABCDEF123456"])
def test_invalid_reaper_target_fails_before_cluster_reads(identity):
    kube = Mock()
    with pytest.raises(PodgroveError, match="--environment"):
        reaper.reap(kube, identity=identity)
    kube.get.assert_not_called()


def without_lease(kube, resources=None):
    resources = resources or {}
    def get(kind, name=None, **kwargs):
        if name is None:
            assert kind == "configmap" and kwargs.get("selector")
            return {"items": []}
        return resources.get((kind, name), {})
    kube.get.side_effect = get


def owned_resource(data, kind="pvc", *, legacy=False):
    name = f"pg-{data['identity']}" + ("-0" if kind == "pod" and not legacy else "")
    return name, {"metadata": {"name": name, "namespace": data["namespace"],
                               "labels": {MANAGED: "podgrove", ENVIRONMENT: data["identity"]}}}


def test_absent_cluster_environment_cleans_local_only_without_cluster_writes(tmp_path, monkeypatch):
    root, path, data, _, kube, stop = environment(tmp_path, monkeypatch)
    without_lease(kube)
    path.with_suffix(".log").write_text("old log")
    assert reaper.reap(kube) == [{"identity": data["identity"], "reason": "cluster environment already absent", "deleted": True}]
    assert root.exists() and not path.parent.exists()
    kube.destroy.assert_not_called()
    stop.assert_called_once()
    exact = [(call.args[0], call.args[1]) for call in kube.get.call_args_list if len(call.args) > 1]
    assert set(exact) == {("configmap", f"pg-{data['identity']}"), ("statefulset", f"pg-{data['identity']}"),
                          ("pod", f"pg-{data['identity']}-0"), ("pod", f"pg-{data['identity']}"),
                          ("pvc", f"pg-{data['identity']}"), ("networkpolicy", f"pg-{data['identity']}"),
                          ("service", f"pg-{data['identity']}")}
    assert len(exact) == 14, "absence must be rechecked under the local lock"


def test_absent_cluster_environment_dry_run_does_not_create_lock_or_remove_files(tmp_path, monkeypatch):
    _, path, data, _, kube, stop = environment(tmp_path, monkeypatch)
    without_lease(kube)
    assert reaper.reap(kube, dry_run=True) == [{"identity": data["identity"], "reason": "cluster environment already absent", "deleted": False}]
    assert path.exists() and not path.with_suffix(".lock").exists()
    stop.assert_not_called()
    kube.destroy.assert_not_called()


@pytest.mark.parametrize("kind,legacy", [("pvc", False), ("pod", True), ("statefulset", False)])
def test_missing_lease_with_fresh_owned_resources_preserves_retry_state(tmp_path, monkeypatch, kind, legacy):
    _, path, data, _, kube, stop = environment(tmp_path, monkeypatch)
    name, resource = owned_resource(data, kind, legacy=legacy)
    without_lease(kube, {(kind, name): resource})
    result = reaper.reap(kube)
    assert result[0]["deleted"] is False and "lease is missing" in result[0]["error"]
    assert path.exists()
    stop.assert_not_called()
    kube.destroy.assert_not_called()


@pytest.mark.parametrize("why", ["expired", "missing"])
def test_missing_lease_with_local_expiry_or_missing_worktree_deletes_owned_remainders(tmp_path, monkeypatch, why):
    root, path, data, _, kube, stop = environment(tmp_path, monkeypatch)
    if why == "expired":
        data["last_activity"] = 100
        state.write(path, data)
    else:
        root.rmdir()
    name, resource = owned_resource(data)
    without_lease(kube, {("pvc", name): resource})
    result = reaper.reap(kube)
    assert result[0]["deleted"] is True
    assert result[0]["reason"] == ("idle TTL expired" if why == "expired" else "local worktree no longer exists")
    stop.assert_called_once()
    kube.destroy.assert_called_once_with(data["identity"], namespace_mode="shared")
    assert not path.exists()


def test_no_lease_foreign_resource_collision_blocks_local_cleanup(tmp_path, monkeypatch):
    root, path, data, _, kube, stop = environment(tmp_path, monkeypatch)
    root.rmdir()
    name, resource = owned_resource(data)
    resource["metadata"]["labels"][MANAGED] = "another-controller"
    without_lease(kube, {("pvc", name): resource})
    result = reaper.reap(kube)
    assert result[0]["deleted"] is False and "foreign" in result[0]["error"]
    assert path.exists()
    stop.assert_not_called()
    kube.destroy.assert_not_called()


def test_lease_appearing_before_locked_absence_check_preserves_local_state(tmp_path, monkeypatch):
    _, path, _, lease, kube, stop = environment(tmp_path, monkeypatch)
    checks = 0
    def get(kind, name=None, **kwargs):
        nonlocal checks
        if name is None:
            return {"items": []}
        if kind == "configmap":
            checks += 1
            return lease if checks == 2 else {}
        return {}
    kube.get.side_effect = get
    result = reaper.reap(kube)
    assert "lease exists or appeared" in result[0]["error"] and path.exists()
    stop.assert_not_called()
    kube.destroy.assert_not_called()


@pytest.mark.parametrize("failure", ["stop", "destroy"])
def test_local_only_reap_failure_preserves_binding_and_log(tmp_path, monkeypatch, failure):
    root, path, data, _, kube, stop = environment(tmp_path, monkeypatch)
    root.rmdir()
    path.with_suffix(".log").write_text("evidence")
    name, resource = owned_resource(data)
    without_lease(kube, {("pvc", name): resource})
    target = stop if failure == "stop" else kube.destroy
    target.side_effect = PodgroveError("temporary failure")
    assert reaper.reap(kube)[0]["deleted"] is False
    assert path.exists() and path.with_suffix(".log").read_text() == "evidence"
    assert not path.with_suffix(".lock").exists()
    if failure == "stop":
        kube.destroy.assert_not_called()


def test_targeted_local_only_reap_ignores_other_identity_context_and_namespace(tmp_path, monkeypatch):
    _, path, data, _, kube, _ = environment(tmp_path, monkeypatch)
    records = []
    for index, change in enumerate(({"context": "other-cluster"}, {"namespace": "podgrove-testing"}, {})):
        root = tmp_path / f"other-worktree-{index}"
        record = {**data, "root": str(root), "identity": state.identity(root), **change}
        record_path = state.state_path(root, record["context"])
        state.write(record_path, record)
        records.append(record_path)
    without_lease(kube)
    assert reaper.reap(kube, identity=data["identity"])[0]["identity"] == data["identity"]
    assert not path.exists() and all(item.exists() for item in records)
    for call in kube.get.call_args_list:
        if len(call.args) > 1:
            assert data["identity"] in call.args[1]


@pytest.mark.parametrize("mode", ["shared", "worktree", "exclusive"])
def test_reaper_without_lease_uses_saved_mode_for_owned_remainders(tmp_path, monkeypatch, mode):
    _, path, data, _, kube, stop = environment(tmp_path, monkeypatch)
    data.update(namespace="wt-selected", namespace_mode=mode, last_activity=100)
    state.write(path, data)
    kube.namespace = data["namespace"]
    name, resource = owned_resource(data)
    without_lease(kube, {("pvc", name): resource})
    assert reaper.reap(kube)[0]["deleted"] is True
    kube.destroy.assert_called_once_with(data["identity"], namespace_mode=mode)
    stop.assert_called_once()
    assert not path.exists()


def test_reaper_without_lease_discards_empty_legacy_state_without_namespace_access(tmp_path, monkeypatch):
    _, path, data, _, kube, _ = environment(tmp_path, monkeypatch)
    data["namespace"] = "wt-legacy"
    state.write(path, data)
    kube.namespace = data["namespace"]
    without_lease(kube)
    assert reaper.reap(kube)[0]["deleted"] is True
    kube.destroy.assert_not_called()
    assert not path.exists()


def test_reaper_refuses_local_and_cluster_mode_disagreement_before_stopping(tmp_path, monkeypatch):
    _, path, data, _, kube, stop = environment(tmp_path, monkeypatch)
    data["namespace_mode"] = "worktree"
    state.write(path, data)
    result = reaper.reap(kube)
    assert not result[0]["deleted"] and "namespace_mode disagree" in result[0]["error"]
    kube.destroy.assert_not_called()
    stop.assert_not_called()
    assert path.exists()
