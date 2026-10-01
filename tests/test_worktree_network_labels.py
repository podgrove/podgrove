import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube, engine_pod_manifest, manifests
from podgrove.repository import WORKTREE_NAME, worktree_name

IDENT = "123456abcdef"
OTHER = "abcdef123456"
NAMESPACE = "team-dev"


def environment():
    desired = manifests(NAMESPACE, IDENT, Path("/worktree/web-checkout"), "small", 600, storage_class="approved")
    controller = copy.deepcopy(next(item for item in desired if item["kind"] == "StatefulSet"))
    controller["metadata"].update(uid="controller-uid", resourceVersion="10")
    pod = engine_pod_manifest(controller)
    pod["metadata"].update(uid="pod-uid", resourceVersion="11")
    budget = copy.deepcopy(next(item for item in desired if item["kind"] == "PodDisruptionBudget"))
    budget["metadata"].update(uid="budget-uid", resourceVersion="12")
    objects = {("StatefulSet", f"pg-{IDENT}"): controller, ("pod", f"pg-{IDENT}-0"): pod,
               ("PodDisruptionBudget", f"pg-{IDENT}"): budget}
    kube = Kube("offline-context", NAMESPACE)
    kube.get = Mock(side_effect=lambda kind, name: copy.deepcopy(objects.get((kind, name), {})))
    kube.call = Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
    return kube, desired, objects


@pytest.mark.parametrize("name", ["web-checkout", "Web.Mixed_7", "directory with spaces", "x" * 80, "日本語"])
def test_new_engine_worktree_label_is_only_on_pod_template_and_derived_pod(name):
    root = Path("/worktree") / name
    resources = manifests(NAMESPACE, IDENT, root, "small", 600)
    controller = next(item for item in resources if item["kind"] == "StatefulSet")
    assert controller["spec"]["template"]["metadata"]["labels"][WORKTREE_NAME] == worktree_name(root)
    assert engine_pod_manifest(controller)["metadata"]["labels"][WORKTREE_NAME] == worktree_name(root)
    assert all(WORKTREE_NAME not in item["metadata"]["labels"] for item in resources)
    assert WORKTREE_NAME not in controller["spec"]["selector"]["matchLabels"]
    assert controller["spec"]["updateStrategy"] == {"type": "OnDelete"}


def test_disabled_default_policy_bytes_and_lease_keys_remain_legacy_compatible(monkeypatch):
    monkeypatch.setenv("PODGROVE_OWNER", "owner")
    monkeypatch.setenv("PODGROVE_REPO", "project")
    monkeypatch.setenv("PODGROVE_BRANCH", "main")
    resources = manifests(NAMESPACE, IDENT, Path("/worktree/web-checkout"), "small", 600)
    policy = next(item for item in resources if item["kind"] == "NetworkPolicy")
    expected = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": f"pg-{IDENT}", "namespace": NAMESPACE,
                             "labels": {MANAGED: "podgrove", ENVIRONMENT: IDENT, "podgrove.dev/node-mode": "shared",
                                        "podgrove.dev/worktree": IDENT, "podgrove.dev/repo": "project",
                                        "podgrove.dev/owner": "owner", "podgrove.dev/branch": "main"}},
                "spec": {"podSelector": {"matchLabels": {MANAGED: "podgrove", ENVIRONMENT: IDENT}},
                         "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": [
                             {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                      "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                              "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
                             {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [
                                 "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
                                 "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
                                 "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/3"]}}],
                              "ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 80}]}]}}
    assert json.dumps(policy, sort_keys=True) == json.dumps(expected, sort_keys=True)
    explicit = manifests(NAMESPACE, IDENT, Path("/worktree/web-checkout"), "small", 600,
                         network={"pod_to_pod": "disabled"})
    assert next(item for item in explicit if item["kind"] == "NetworkPolicy") == policy
    for rendered in (resources, explicit):
        lease = next(item for item in rendered if item["kind"] == "ConfigMap")
        assert set(lease["data"]) == {"root", "last_activity", "ttl_seconds", "mr_url", "namespace_mode"}


@pytest.mark.parametrize("mode", ["open", "selected"])
def test_initial_peer_profile_has_no_unverified_publications_or_pod_identity(mode):
    resources = manifests(NAMESPACE, IDENT, Path("/worktree/web-checkout"), "small", 600,
                          network={"pod_to_pod": mode})
    lease = next(item for item in resources if item["kind"] == "ConfigMap")
    profile = json.loads(lease["data"]["pod_network"])
    assert profile["version"] == 1
    assert profile["worktree"] == "web-checkout"
    assert profile["network"]["pod_to_pod"] == mode
    assert profile["ports"] == []
    assert profile["pod_uid"] is None


@pytest.mark.parametrize("previous", [None, "old-checkout"])
def test_worktree_label_upgrade_is_guarded_metadata_only_and_preserves_other_labels(previous):
    kube, desired, objects = environment()
    controller, pod = objects[("StatefulSet", f"pg-{IDENT}")], objects[("pod", f"pg-{IDENT}-0")]
    for target in (controller["spec"]["template"]["metadata"], pod["metadata"]):
        target["labels"].pop(WORKTREE_NAME)
        target["labels"]["operator.example/keep"] = "yes"
        if previous:
            target["labels"][WORKTREE_NAME] = previous
    before = copy.deepcopy(objects)
    kube.reconcile_engine_protection(desired, IDENT)
    assert len(kube.call.call_args_list) == 2
    for call, kind, obj in zip(kube.call.call_args_list, ("statefulset", "pod"), (controller, pod)):
        assert call.args[:5] == ("patch", kind, obj["metadata"]["name"], "--type=json", "-p")
        target = "/spec/template/metadata/labels/" if kind == "statefulset" else "/metadata/labels/"
        assert json.loads(call.args[5]) == [
            {"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]},
            {"op": "add", "path": target + "podgrove.dev~1worktree-name", "value": "web-checkout"}]
    assert before == objects


def test_label_and_annotation_upgrade_share_one_resource_version_patch_per_object():
    kube, desired, objects = environment()
    for target in (objects[("StatefulSet", f"pg-{IDENT}")]["spec"]["template"]["metadata"],
                   objects[("pod", f"pg-{IDENT}-0")]["metadata"]):
        target["labels"].pop(WORKTREE_NAME)
        target.pop("annotations")
    kube.reconcile_engine_protection(desired, IDENT)
    assert len(kube.call.call_args_list) == 2
    for call in kube.call.call_args_list:
        patch = json.loads(call.args[5])
        assert len(patch) == 4
        assert [item["op"] for item in patch] == ["test", "test", "add", "add"]
        assert patch[2]["path"].endswith("/annotations")
        assert patch[3]["path"].endswith("/labels/podgrove.dev~1worktree-name")


def test_worktree_label_repair_never_retries_a_replacement_or_version_conflict():
    kube, desired, objects = environment()
    objects[("StatefulSet", f"pg-{IDENT}")]["spec"]["template"]["metadata"]["labels"].pop(WORKTREE_NAME)
    objects[("pod", f"pg-{IDENT}-0")]["metadata"]["labels"].pop(WORKTREE_NAME)
    kube.call.side_effect = PodgroveError("UID test failed")
    with pytest.raises(PodgroveError, match="UID test failed"):
        kube.reconcile_engine_protection(desired, IDENT)
    kube.call.assert_called_once()
    assert kube.call.call_args.args[:2] == ("patch", "statefulset")


def test_unchanged_worktree_labels_and_protection_never_write():
    kube, desired, _ = environment()
    kube.reconcile_engine_protection(desired, IDENT)
    kube.call.assert_not_called()


@pytest.mark.parametrize("invalid", [None, "", "not/a/label", "x" * 64, []])
def test_invalid_desired_label_refuses_all_engine_mutations(invalid):
    kube, desired, _ = environment()
    next(item for item in desired if item["kind"] == "StatefulSet")["spec"]["template"]["metadata"]["labels"][WORKTREE_NAME] = invalid
    with pytest.raises(PodgroveError, match="stable worktree-name"):
        kube.reconcile_engine_protection(desired, IDENT)
    kube.call.assert_not_called()


def test_label_repair_refuses_a_pod_belonging_to_a_different_controller():
    kube, desired, objects = environment()
    pod = objects[("pod", f"pg-{IDENT}-0")]
    pod["metadata"]["labels"].pop(WORKTREE_NAME)
    pod["metadata"]["ownerReferences"][0]["uid"] = "replacement-controller"
    with pytest.raises(PodgroveError, match="not controlled"):
        kube.reconcile_engine_protection(desired, IDENT)
    kube.call.assert_not_called()


def policy(source=OTHER, target=OTHER):
    return {"metadata": {"name": "legacy-link", "namespace": NAMESPACE,
                         "labels": {MANAGED: "podgrove", ENVIRONMENT: source, "podgrove.dev/component": "connection"}},
            "spec": {"podSelector": {"matchLabels": {MANAGED: "podgrove", ENVIRONMENT: target}}}}


@pytest.mark.parametrize("source,target", [(IDENT, OTHER), (OTHER, IDENT), (IDENT, IDENT)])
def test_legacy_policy_owner_and_target_both_block_without_writes(source, target):
    kube = Kube("offline-context", NAMESPACE)
    kube.get = Mock(return_value={"items": [policy(source, target)]})
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="recorded older Podgrove version's scoped down"):
        kube.refuse_legacy_connections(IDENT)
    kube.get.assert_called_once_with("networkpolicies", selector=f"{MANAGED}=podgrove,podgrove.dev/component=connection", ignore_missing=False)
    kube.call.assert_not_called()


def test_legacy_inventory_is_one_read_in_explicit_namespace_without_mutation():
    kube = Kube("offline-context", NAMESPACE)
    kube.call = Mock(return_value=SimpleNamespace(stdout=json.dumps({"items": [policy()]})))
    kube.refuse_legacy_connections(IDENT)
    kube.call.assert_called_once_with("get", "networkpolicies", "-l",
                                      f"{MANAGED}=podgrove,podgrove.dev/component=connection",
                                      "-o", "json")
    assert kube.command(*kube.call.call_args.args)[:5] == ["kubectl", "--context", "offline-context", "--namespace", NAMESPACE]


def test_empty_legacy_list_keeps_json_output_by_omitting_ignore_not_found():
    kube = Kube("offline-context", NAMESPACE)
    def response(*args, **kwargs):
        return SimpleNamespace(stdout="" if "--ignore-not-found" in args else json.dumps({"kind": "List", "items": []}))
    kube.call = Mock(side_effect=response)
    kube.refuse_legacy_connections(IDENT)
    kube.call.assert_called_once_with("get", "networkpolicies", "-l",
                                      f"{MANAGED}=podgrove,podgrove.dev/component=connection", "-o", "json")


@pytest.mark.parametrize("response", ["", " ", "not json", '{"items":'])
def test_legacy_list_still_refuses_empty_or_malformed_json_output(response):
    kube = Kube("offline-context", NAMESPACE)
    kube.call = Mock(return_value=SimpleNamespace(stdout=response))
    with pytest.raises(PodgroveError, match="Cannot verify"):
        kube.refuse_legacy_connections(IDENT)
    assert kube.call.call_count == 1


@pytest.mark.parametrize("selector", [
    {}, {"matchLabels": {}}, {"matchLabels": {MANAGED: "podgrove"}},
    {"matchLabels": {ENVIRONMENT: OTHER}},
    {"matchLabels": {MANAGED: "podgrove", ENVIRONMENT: OTHER}, "matchExpressions": []},
    {"matchExpressions": [{"key": ENVIRONMENT, "operator": "In", "values": [IDENT]}]},
    {"matchExpressions": [{"key": ENVIRONMENT, "operator": "NotIn", "values": [OTHER]}]},
    {"matchLabels": {MANAGED: "podgrove"}, "matchExpressions": [{"key": ENVIRONMENT, "operator": "Exists"}]},
    {"matchLabels": {MANAGED: "podgrove", ENVIRONMENT: OTHER, "operator.example/custom": "yes"}},
    {"matchLabels": {MANAGED: "podgrove", ENVIRONMENT: "invalid-identity"}},
    {"matchLabels": {MANAGED: "other", ENVIRONMENT: OTHER}},
])
def test_broad_or_unsupported_legacy_selector_cannot_be_assumed_to_target_another_engine(selector):
    kube = Kube("offline-context", NAMESPACE)
    resource = policy()
    resource["spec"]["podSelector"] = selector
    kube.get, kube.call = Mock(return_value={"items": [resource]}), Mock()
    with pytest.raises(PodgroveError, match="selector is broad or unsupported"):
        kube.refuse_legacy_connections(IDENT)
    kube.get.assert_called_once()
    kube.call.assert_not_called()


@pytest.mark.parametrize("inventory", [{}, [], {"items": None}, {"items": [None]}, {"items": [{}]},
                                        {"items": [{"metadata": {}, "spec": {}}]}])
def test_malformed_legacy_inventory_is_not_silently_treated_as_empty(inventory):
    kube = Kube("offline-context", NAMESPACE)
    kube.get, kube.call = Mock(return_value=inventory), Mock()
    with pytest.raises(PodgroveError, match="legacy connection|Legacy connection"):
        kube.refuse_legacy_connections(IDENT)
    kube.call.assert_not_called()


def test_legacy_read_error_stops_instead_of_returning_permission_to_start():
    kube = Kube("offline-context", NAMESPACE)
    kube.get, kube.call = Mock(side_effect=PodgroveError("Forbidden")), Mock()
    with pytest.raises(PodgroveError, match="Forbidden"):
        kube.refuse_legacy_connections(IDENT)
    kube.call.assert_not_called()


def test_cross_namespace_legacy_response_is_refused_without_following_it():
    kube = Kube("offline-context", NAMESPACE)
    foreign = policy()
    foreign["metadata"]["namespace"] = "other-namespace"
    kube.get, kube.call = Mock(return_value={"items": [foreign]}), Mock()
    with pytest.raises(PodgroveError, match="foreign or malformed"):
        kube.refuse_legacy_connections(IDENT)
    assert kube.get.call_count == 1
    kube.call.assert_not_called()
