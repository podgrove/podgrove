import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from podgrove.bootstrap import render_bootstrap
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube, engine_pod_manifest, manifests

IDENT = "123456abcdef"
ANNOTATIONS = {"cluster-autoscaler.kubernetes.io/safe-to-evict": "false", "autoscaling.cast.ai/removal-disabled": "true"}


def environment():
    desired = manifests("team-dev", IDENT, Path("/worktree/project"), "small", 600, storage_class="approved")
    controller = copy.deepcopy(next(item for item in desired if item["kind"] == "StatefulSet"))
    controller["metadata"].update(uid="controller-uid", resourceVersion="10")
    pod = engine_pod_manifest(controller)
    pod["metadata"].update(uid="pod-uid", resourceVersion="11")
    budget = copy.deepcopy(next(item for item in desired if item["kind"] == "PodDisruptionBudget"))
    budget["metadata"].update(uid="budget-uid", resourceVersion="12")
    objects = {("StatefulSet", f"pg-{IDENT}"): controller, ("pod", f"pg-{IDENT}-0"): pod,
               ("PodDisruptionBudget", f"pg-{IDENT}"): budget}
    kube = Kube("offline-context", "team-dev")
    kube.get = Mock(side_effect=lambda kind, name: copy.deepcopy(objects.get((kind, name), {})))
    kube.call = Mock(return_value=SimpleNamespace(returncode=0, stdout="yes"))
    return kube, desired, objects


def test_engine_eviction_budget_protects_only_its_one_healthy_replica():
    _, desired, _ = environment()
    controller = next(item for item in desired if item["kind"] == "StatefulSet")
    pod = engine_pod_manifest(controller)
    budget = next(item for item in desired if item["kind"] == "PodDisruptionBudget")
    assert pod["metadata"]["annotations"] == ANNOTATIONS
    assert budget["apiVersion"] == "policy/v1"
    selector = budget["spec"]["selector"]["matchLabels"]
    candidates = [pod, {"metadata": {"labels": {MANAGED: "podgrove", ENVIRONMENT: "abcdef123456"}}},
                  {"metadata": {"labels": {MANAGED: "foreign", ENVIRONMENT: IDENT}}}, {"metadata": {"labels": {}}}]
    protected = [candidate for candidate in candidates if all(candidate["metadata"]["labels"].get(k) == v for k, v in selector.items())]
    assert protected == [pod]
    assert controller["spec"]["replicas"] - budget["spec"]["maxUnavailable"] == 1
    assert desired.index(budget) < desired.index(controller)


@pytest.mark.parametrize("mutation", ["template-autoscaler", "template-cast", "pod-autoscaler", "pod-cast", "missing-budget", "budget-permits-eviction", "empty-selector", "other-environment"])
def test_removing_each_eviction_safeguard_is_detected_without_any_write(mutation):
    kube, desired, objects = environment()
    kube.check_engine_protection(desired, IDENT)
    if mutation.startswith(("template-", "pod-")):
        target = objects[("StatefulSet", f"pg-{IDENT}")]["spec"]["template"] if mutation.startswith("template") else objects[("pod", f"pg-{IDENT}-0")]
        key = "cluster-autoscaler.kubernetes.io/safe-to-evict" if mutation.endswith("autoscaler") else "autoscaling.cast.ai/removal-disabled"
        target["metadata"]["annotations"].pop(key)
    elif mutation == "missing-budget":
        objects.pop(("PodDisruptionBudget", f"pg-{IDENT}"))
    elif mutation == "budget-permits-eviction":
        objects[("PodDisruptionBudget", f"pg-{IDENT}")]["spec"]["maxUnavailable"] = 1
    elif mutation == "empty-selector":
        objects[("PodDisruptionBudget", f"pg-{IDENT}")]["spec"]["selector"] = {}
    else:
        objects[("PodDisruptionBudget", f"pg-{IDENT}")]["spec"]["selector"]["matchLabels"][ENVIRONMENT] = "abcdef123456"
    with pytest.raises(PodgroveError, match="eviction protection is missing or changed"):
        kube.check_engine_protection(desired, IDENT)
    kube.call.assert_not_called()


def test_legacy_annotations_upgrade_is_uid_version_guarded_metadata_only():
    kube, desired, objects = environment()
    controller = objects[("StatefulSet", f"pg-{IDENT}")]
    pod = objects[("pod", f"pg-{IDENT}-0")]
    controller["spec"]["template"]["metadata"].pop("annotations")
    pod["metadata"]["annotations"] = {"operator.example/keep": "yes"}
    objects.pop(("PodDisruptionBudget", f"pg-{IDENT}"))
    before = copy.deepcopy(objects)
    kube.reconcile_engine_protection(desired, IDENT)
    calls = kube.call.call_args_list
    assert calls[0].args == ("create", "-f", "-")
    assert json.loads(calls[0].kwargs["input"])["kind"] == "PodDisruptionBudget"
    for call, kind, obj in zip(calls[1:], ("statefulset", "pod"), (controller, pod)):
        assert call.args[:5] == ("patch", kind, obj["metadata"]["name"], "--type=json", "-p")
        patch = json.loads(call.args[5])
        assert patch[:2] == [{"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]},
                             {"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]}]
        assert all(operation["op"] == "add" and "/annotations" in operation["path"] for operation in patch[2:])
    assert len(calls) == 3
    assert objects == before


@pytest.mark.parametrize("kind", ["StatefulSet", "pod", "PodDisruptionBudget"])
@pytest.mark.parametrize("mutation", ["foreign-owner", "wrong-namespace", "wrong-name", "deleting", "missing-uid", "missing-version"])
def test_legacy_upgrade_refuses_foreign_or_incomplete_objects_before_any_mutation(kind, mutation):
    kube, desired, objects = environment()
    objects[("StatefulSet", f"pg-{IDENT}")]["spec"]["template"]["metadata"].pop("annotations")
    key = (kind, f"pg-{IDENT}-0" if kind == "pod" else f"pg-{IDENT}")
    metadata = objects[key]["metadata"]
    if mutation == "foreign-owner":
        metadata["labels"][MANAGED] = "other"
    elif mutation == "wrong-namespace":
        metadata["namespace"] = "other"
    elif mutation == "wrong-name":
        metadata["name"] = "other"
    elif mutation == "deleting":
        metadata["deletionTimestamp"] = "now"
    else:
        metadata.pop("uid" if mutation == "missing-uid" else "resourceVersion")
    with pytest.raises(PodgroveError, match="protection target"):
        kube.reconcile_engine_protection(desired, IDENT)
    kube.call.assert_not_called()


def test_changed_engine_settings_cannot_be_covered_by_annotation_migration():
    kube, desired, objects = environment()
    objects[("StatefulSet", f"pg-{IDENT}")]["spec"]["template"]["spec"]["nodeSelector"]["example.com/pool"] = "other"
    desired[-1]["spec"]["template"]["spec"]["nodeSelector"]["example.com/pool"] = "wanted"
    with pytest.raises(PodgroveError, match="different engine settings"):
        kube.reconcile_engine_protection(desired, IDENT)
    kube.call.assert_not_called()


def test_owned_budget_repair_pins_uid_and_version_and_does_not_patch_protected_pods():
    kube, desired, objects = environment()
    objects[("PodDisruptionBudget", f"pg-{IDENT}")]["spec"]["maxUnavailable"] = 1
    kube.reconcile_engine_protection(desired, IDENT)
    kube.call.assert_called_once()
    call = kube.call.call_args
    assert call.args == ("replace", "-f", "-")
    body = json.loads(call.kwargs["input"])
    assert body["metadata"]["uid"] == "budget-uid"
    assert body["metadata"]["resourceVersion"] == "12"
    assert body["spec"]["maxUnavailable"] == 0


def test_unchanged_protection_never_writes():
    kube, desired, _ = environment()
    kube.reconcile_engine_protection(desired, IDENT)
    kube.call.assert_not_called()


@pytest.mark.parametrize("verb", ["get", "list", "create", "update", "delete"])
def test_doctor_detects_missing_budget_permission_without_inventory_reads(verb):
    kube, _, _ = environment()
    kube.call.side_effect = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="no" if args == ("auth", "can-i", verb, "poddisruptionbudgets.policy") else "yes")
    with pytest.raises(PodgroveError, match=f"{verb} poddisruptionbudgets.policy"):
        kube.preflight()
    kube.get.assert_not_called()


def test_bootstrap_client_and_reaper_get_only_the_required_namespaced_budget_verbs():
    docs = [doc for file in render_bootstrap("team-dev").values() for doc in file]
    rules = {doc["metadata"]["name"]: doc["rules"] for doc in docs if doc["kind"] == "Role"}
    for role, verbs in (("podgrove-client", {"get", "list", "watch", "create", "patch", "update", "delete"}),
                        ("podgrove-reaper", {"get", "list", "watch", "delete"})):
        matches = [rule for rule in rules[role] if rule["apiGroups"] == ["policy"]]
        assert len(matches) == 1
        assert matches[0]["resources"] == ["poddisruptionbudgets"]
        assert set(matches[0]["verbs"]) == verbs
    assert all(doc["metadata"]["namespace"] == "team-dev" for doc in docs)


def test_budget_admission_denial_leaves_no_persisted_supporting_resources():
    kube, desired, _ = environment()
    kube.get = Mock(return_value={})
    calls = []
    def call(*args, **kwargs):
        submitted = json.loads(kwargs["input"])
        calls.append((args, submitted["kind"]))
        assert "--dry-run=server" in args
        if submitted["kind"] == "PodDisruptionBudget":
            raise PodgroveError("policy denied")
    kube.call = call
    with pytest.raises(PodgroveError, match="PodDisruptionBudget"):
        kube.create_environment(desired, IDENT)
    assert calls == [(("create", "--dry-run=server", "-f", "-"), "Pod"),
                     (("create", "--dry-run=server", "-f", "-"), "PodDisruptionBudget")]


def test_patch_conflict_is_not_replayed_or_followed_by_a_second_pod_patch():
    kube, desired, objects = environment()
    objects[("StatefulSet", f"pg-{IDENT}")]["spec"]["template"]["metadata"].pop("annotations")
    objects[("pod", f"pg-{IDENT}-0")]["metadata"].pop("annotations")
    kube.call.side_effect = PodgroveError("UID test failed")
    with pytest.raises(PodgroveError, match="UID test failed"):
        kube.reconcile_engine_protection(desired, IDENT)
    kube.call.assert_called_once()
    assert kube.call.call_args.args[:2] == ("patch", "statefulset")


def test_reaper_stale_local_cleanup_detects_owned_budget_even_after_lease_is_gone():
    from podgrove.reaper import _remaining_without_lease
    kube, _, objects = environment()
    kube.get = Mock(side_effect=lambda kind, name=None, **kwargs: objects[("PodDisruptionBudget", f"pg-{IDENT}")]
                    if kind == "poddisruptionbudget" else {})
    assert _remaining_without_lease(kube, IDENT) == [f"poddisruptionbudget/pg-{IDENT}"]
    kube.call.assert_not_called()
