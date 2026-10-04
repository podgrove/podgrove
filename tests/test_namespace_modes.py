import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from podgrove import reaper, state
from podgrove.errors import PodgroveError
from podgrove.bootstrap import PROVISIONING_MARKER, provisioning_marker
from podgrove.kube import ENVIRONMENT, MANAGED, Kube, manifests, namespace_name, resolve_namespace


IDENT = "123456abcdef"


def lease(namespace, mode="shared", ident=IDENT):
    data = {"last_activity": "100", "ttl_seconds": "1"}
    if mode is not None:
        data["namespace_mode"] = mode
    return {"kind": "ConfigMap", "metadata": {"uid": "lease-uid", "name": f"pg-{ident}", "namespace": namespace, "resourceVersion": "1",
                         "labels": {MANAGED: "podgrove", ENVIRONMENT: ident}}, "data": data}


def fake_kube(namespace, mode, current=None, labels=None):
    kube = Kube("chosen-context", namespace, namespace_mode=mode)
    marker_mode = mode if mode in ("shared", "worktree") else "worktree"
    marker = provisioning_marker(namespace, marker_mode, IDENT if marker_mode == "worktree" else None)
    if labels is not None:
        marker["metadata"]["labels"] = labels
    def get(kind, name=None, **kwargs):
        assert kind == "configmap", "Lifecycle must not inspect cluster-scoped objects"
        return marker if name == PROVISIONING_MARKER else current or {}
    kube.get = Mock(side_effect=get)
    remaining = [current] if current else []
    def transport(*args, **kwargs):
        if args[0] == "delete":
            assert args[1] == "--raw" and args[2] == f"/api/v1/namespaces/{namespace}/configmaps/pg-{IDENT}"
            options = json.loads(kwargs["input"])
            assert options["preconditions"] == {key: current["metadata"][key] for key in ("uid", "resourceVersion")}
            remaining.clear()
            return SimpleNamespace(stdout="", returncode=0)
        assert args[0] == "get"
        value = {"items": remaining} if "-l" in args else (remaining[0] if remaining else None)
        return SimpleNamespace(stdout=json.dumps(value) if value is not None else "", returncode=0)
    kube.call = Mock(side_effect=transport)
    return kube, marker


@pytest.mark.parametrize("name", ["a", "1", "app-development", "wt-explicit-shared", "x" * 63])
def test_any_valid_namespace_is_accepted(name):
    assert namespace_name(name) == name


@pytest.mark.parametrize("name", [None, "", "Upper", "-bad", "bad-", "a.b", "a/b", "x" * 64, "a\n", 7])
def test_invalid_namespace_fails_before_cluster_access(name):
    with pytest.raises(PodgroveError, match="namespace"):
        Kube("context", name)


def test_worktree_names_are_deterministic_bounded_and_distinct_for_long_bases():
    assert resolve_namespace("team", "shared", IDENT) == "team"
    assert resolve_namespace("team", "worktree", IDENT) == f"team-wt-{IDENT}"
    base = "x" * 62
    first = resolve_namespace(base + "a", "worktree", IDENT)
    second = resolve_namespace(base + "b", "worktree", IDENT)
    assert first != second and len(first) <= 63 and len(second) <= 63
    assert first == resolve_namespace(base + "a", "worktree", IDENT)
    assert first != resolve_namespace(base + "a", "worktree", "abcdef123456")
    assert namespace_name(first) == first


@pytest.mark.parametrize("mode", ["exclusive", "unknown", None])
def test_new_namespace_resolution_rejects_legacy_or_unspecified_modes(mode):
    with pytest.raises(PodgroveError, match="namespace_mode"):
        resolve_namespace("team", mode, IDENT)


@pytest.mark.parametrize("namespace", ["team-development", "wt-selected"])
def test_shared_marker_is_verified_without_namespace_reads_or_metadata_changes(namespace):
    kube, marker = fake_kube(namespace, "shared", lease(namespace))
    before = copy.deepcopy(marker)
    assert kube.ensure_namespace(IDENT) is False
    kube.call.assert_not_called()
    kube.get.assert_called_once_with("configmap", PROVISIONING_MARKER)
    kube.destroy(IDENT)
    assert marker == before
    assert len([call for call in kube.call.call_args_list if call.args[0] == "delete"]) == 1
    assert all(f"{MANAGED}=podgrove,{ENVIRONMENT}={IDENT}" in call.args
               for call in kube.call.call_args_list if "-l" in call.args)


@pytest.mark.parametrize("owner", [IDENT, "abcdef123456", ""])
def test_shared_mode_cannot_repurpose_worktree_marker(owner):
    kube, marker = fake_kube("wt-selected", "shared", lease("wt-selected"))
    marker["data"]["environment"] = owner
    with pytest.raises(PodgroveError, match="marker identity"):
        kube.ensure_namespace(IDENT)
    kube.call.assert_not_called()
    # Existing owned resources remain cleanable when bootstrap metadata is wrong.
    kube.destroy(IDENT)
    assert len([call for call in kube.call.call_args_list if call.args[0] == "delete"]) == 1


@pytest.mark.parametrize("mode", ["shared", "worktree"])
def test_missing_bootstrap_never_creates_namespace_or_marker(mode):
    kube = Kube("context", "wt-test", namespace_mode=mode)
    kube.get = Mock(return_value={})
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="Regenerate the namespaced bootstrap"):
        kube.ensure_namespace(IDENT)
    kube.call.assert_not_called()


def test_legacy_exclusive_start_requires_migration_without_cluster_access():
    kube, _ = fake_kube("wt-test", "exclusive")
    with pytest.raises(PodgroveError, match="Legacy exclusive"):
        kube.ensure_namespace(IDENT)
    kube.get.assert_not_called()
    kube.call.assert_not_called()


def test_bootstrapped_worktree_namespace_is_owned_but_retained():
    name = resolve_namespace("team", "worktree", IDENT)
    kube, marker = fake_kube(name, "worktree", lease(name, "worktree"))
    before = copy.deepcopy(marker)
    assert kube.ensure_namespace(IDENT) is False
    kube.destroy(IDENT)
    assert marker == before
    assert all(call.args[1] != "namespace" for call in kube.call.call_args_list)


@pytest.mark.parametrize("change", ["manager", "component", "namespace", "name", "version", "mode", "environment", "label", "terminating", "data"])
def test_worktree_mode_refuses_foreign_or_malformed_marker_before_start(change):
    name = resolve_namespace("team", "worktree", IDENT)
    kube, marker = fake_kube(name, "worktree", lease(name, "worktree"))
    if change in ("manager", "component"):
        marker["metadata"]["labels"][MANAGED if change == "manager" else "podgrove.dev/component"] = "foreign"
    elif change in ("namespace", "name"):
        marker["metadata"][change] = "foreign"
    elif change == "label":
        marker["metadata"]["labels"][ENVIRONMENT] = IDENT
    elif change == "terminating":
        marker["metadata"]["deletionTimestamp"] = "now"
    elif change == "data":
        marker["data"] = []
    else:
        marker["data"]["namespace_mode" if change == "mode" else change] = "foreign"
    with pytest.raises(PodgroveError, match="marker identity"):
        kube.ensure_namespace(IDENT)
    kube.call.assert_not_called()
    kube.destroy(IDENT)
    assert len([call for call in kube.call.call_args_list if call.args[0] == "delete"]) == 1


@pytest.mark.parametrize("mode", ["shared", "worktree", "exclusive"])
def test_lease_manifest_and_state_preserve_explicit_mode(mode, tmp_path):
    root = tmp_path / "project"
    resources = manifests("wt-test", state.identity(root), root, "small", 600, namespace_mode=mode)
    assert next(item for item in resources if item["kind"] == "ConfigMap")["data"]["namespace_mode"] == mode
    data = {"root": str(root), "identity": state.identity(root), "context": "context", "namespace": "wt-test",
            "namespace_mode": mode}
    before = copy.deepcopy(data)
    state.validate_binding(data, root, "context")
    assert state.namespace_mode(data) == mode and data == before


@pytest.mark.parametrize("namespace,mode", [("wt-old", "exclusive"), ("default", "shared"), ("custom", "shared")])
def test_legacy_state_mode_inference_is_non_mutating(namespace, mode):
    data = {"namespace": namespace}
    assert state.namespace_mode(data) == mode
    assert data == {"namespace": namespace}


@pytest.mark.parametrize("mode", [None, "invalid", "", [], {}])
def test_malformed_persisted_mode_is_rejected(mode):
    with pytest.raises(PodgroveError, match="namespace_mode"):
        state.namespace_mode({"namespace": "wt-test", "namespace_mode": mode})


@pytest.mark.parametrize("namespace", ["wt-old", "team"])
def test_legacy_cleanup_retains_namespace_and_does_not_require_marker_or_lease(namespace):
    kube, _ = fake_kube(namespace, "exclusive")
    kube.destroy(IDENT)
    assert all(call.args[0] == "get" for call in kube.call.call_args_list)
    kube.get.assert_not_called()


@pytest.mark.parametrize("change", ["namespace", "name", "manager", "identity", "null-mode", "invalid-mode"])
def test_lease_mode_recovery_refuses_foreign_or_malformed_lease(change):
    current = lease("wt-team", "shared")
    if change in ("namespace", "name"):
        current["metadata"][change] = "foreign"
    elif change in ("manager", "identity"):
        current["metadata"]["labels"][MANAGED if change == "manager" else ENVIRONMENT] = "foreign"
    else:
        current["data"]["namespace_mode"] = None if change == "null-mode" else "unknown"
    kube, _ = fake_kube("wt-team", "shared", current)
    with pytest.raises(PodgroveError):
        kube.destroy(IDENT)
    assert all(call.args[0] == "get" for call in kube.call.call_args_list)


def test_lease_mode_change_refuses_heartbeat_reconnect_and_cleanup():
    current = lease("wt-team", "shared")
    current["metadata"]["uid"] = "lease-uid"
    kube, _ = fake_kube("wt-team", "exclusive", current)
    kube.call.side_effect = None
    kube.call.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(current))
    expected = lease("wt-team", "exclusive")
    expected["kind"] = "ConfigMap"
    for operation in (lambda: kube.heartbeat(IDENT, 200), lambda: kube.destroy(IDENT),
                      lambda: kube._validate_existing(expected, current, IDENT)):
        with pytest.raises(PodgroveError, match="namespace_mode"):
            operation()
    assert all(call.args[0] == "get" for call in kube.call.call_args_list)


@pytest.mark.parametrize("mode", ["shared", "worktree", "exclusive"])
def test_reaper_uses_authoritative_lease_mode_not_namespace_prefix(mode, monkeypatch):
    current = lease("wt-shared", mode)
    kube = Kube("context", "wt-shared", namespace_mode="shared")
    kube.get = Mock(side_effect=[{"items": [current]}, current])
    kube.destroy = Mock()
    monkeypatch.setattr(state, "list_states", lambda *_: [])
    assert reaper.reap(kube)[0]["deleted"] is True
    kube.destroy.assert_called_once_with(IDENT, namespace_mode=mode)


def test_reaper_rechecks_mode_before_stopping_local_or_deleting(monkeypatch):
    current = lease("wt-shared", "shared")
    changed = copy.deepcopy(current)
    changed["data"]["namespace_mode"] = "exclusive"
    kube = Kube("context", "wt-shared", namespace_mode="shared")
    kube.get = Mock(side_effect=[{"items": [current]}, changed])
    kube.destroy = Mock()
    monkeypatch.setattr(state, "list_states", lambda *_: [])
    result = reaper.reap(kube)
    assert not result[0]["deleted"] and "namespace_mode changed" in result[0]["error"]
    kube.destroy.assert_not_called()
