"""Exact policy repair and ownership fences without invoking kubectl."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from podgrove.errors import PodgroveError
from podgrove.bootstrap import PROVISIONING_MARKER, provisioning_marker
from podgrove.kube import ENVIRONMENT, MANAGED, Kube
from podgrove.network import policy_spec

IDENT = "012345abcdef"
NAMESPACE = "team-development"


@pytest.fixture
def setup():
    desired = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
               "metadata": {"name": "pg-" + IDENT, "namespace": NAMESPACE,
                            "labels": {MANAGED: "podgrove", ENVIRONMENT: IDENT}},
               "spec": policy_spec(IDENT, {"blocked_cidrs": ["44.55.0.0/16"]})}
    existing = deepcopy(desired)
    existing["metadata"].update(uid="original-policy", resourceVersion="17")
    namespace = provisioning_marker(NAMESPACE, "shared")
    reads = []
    kube = Kube("offline-test", NAMESPACE, namespace_mode="shared")
    def get(kind, name):
        reads.append((kind, name))
        if kind == "configmap":
            assert name == PROVISIONING_MARKER
            return namespace
        assert kind == "NetworkPolicy" and name == "pg-" + IDENT
        return existing
    kube.get, kube.call = Mock(side_effect=get), Mock()
    return kube, desired, existing, namespace, reads


def test_missing_policy_is_created_only_after_namespace_check(setup):
    kube, desired, existing, _, reads = setup
    existing.clear()
    before = deepcopy(desired)
    kube.reconcile_network_policy([desired], IDENT)
    assert reads == [("configmap", PROVISIONING_MARKER), ("NetworkPolicy", "pg-" + IDENT)]
    assert kube.call.call_args.args == ("create", "-f", "-")
    assert json.loads(kube.call.call_args.kwargs["input"]) == desired == before


def test_api_omitted_empty_rules_and_selector_fields_do_not_replace(setup):
    kube, desired, existing, _, _ = setup
    existing["spec"].pop("ingress")
    existing["spec"]["podSelector"]["matchExpressions"] = []
    existing["spec"]["egress"][0]["to"][0]["namespaceSelector"]["matchExpressions"] = []
    kube.reconcile_network_policy([desired], IDENT)
    kube.call.assert_not_called()


@pytest.mark.parametrize("drift", ["extra-ingress", "extra-egress", "removed-exclusion", "all-pods", "missing-peer-selector"])
def test_repair_replaces_complete_spec_with_uid_and_resource_version(setup, drift):
    kube, desired, existing, _, _ = setup
    if drift == "extra-ingress":
        existing["spec"]["ingress"] = [{}]
    elif drift == "extra-egress":
        existing["spec"]["egress"].append({})
    elif drift == "removed-exclusion":
        existing["spec"]["egress"][1]["to"][0]["ipBlock"]["except"].remove("44.55.0.0/16")
    elif drift == "all-pods":
        existing["spec"]["podSelector"] = {}
    else:
        existing["spec"]["egress"][0]["to"][0].pop("namespaceSelector")
    before = deepcopy(desired)
    kube.reconcile_network_policy([desired], IDENT)
    assert kube.call.call_args.args == ("replace", "-f", "-")
    sent = json.loads(kube.call.call_args.kwargs["input"])
    assert sent["spec"] == desired["spec"]
    assert sent["metadata"]["uid"] == "original-policy"
    assert sent["metadata"]["resourceVersion"] == "17"
    assert desired == before


@pytest.mark.parametrize("change", ["manager", "identity", "name", "namespace", "uid", "version", "terminating"])
def test_foreign_or_unfenced_policy_is_never_replaced_even_if_spec_matches(setup, change):
    kube, desired, existing, _, _ = setup
    meta = existing["metadata"]
    if change in ("manager", "identity"):
        meta["labels"][MANAGED if change == "manager" else ENVIRONMENT] = "foreign"
    elif change in ("name", "namespace"):
        meta[change] = "foreign"
    elif change in ("uid", "version"):
        meta.pop("uid" if change == "uid" else "resourceVersion")
    else:
        meta["deletionTimestamp"] = "2026-09-25T00:00:00Z"
    with pytest.raises(PodgroveError):
        kube.reconcile_network_policy([desired], IDENT)
    kube.call.assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "terminating", "foreign-worktree", "unowned-worktree"])
def test_namespace_must_remain_usable_before_policy_lookup_or_mutation(setup, kind):
    kube, desired, existing, namespace, reads = setup
    existing.clear()
    if kind == "missing":
        namespace.clear()
    elif kind == "terminating":
        namespace["metadata"]["deletionTimestamp"] = "2026-09-25T00:00:00Z"
    else:
        kube.namespace_mode = "worktree"
        namespace["metadata"]["labels"] = {
            MANAGED: "podgrove" if kind == "foreign-worktree" else "foreign",
            "podgrove.dev/component": "bootstrap",
        }
        namespace["data"] = {"version": "1", "namespace_mode": "worktree", "environment": "fedcba543210" if kind == "foreign-worktree" else IDENT}
    with pytest.raises(PodgroveError):
        kube.reconcile_network_policy([desired], IDENT)
    assert reads == [("configmap", PROVISIONING_MARKER)]
    kube.call.assert_not_called()


def test_concurrent_replace_conflict_is_propagated_without_retry_or_adoption(setup):
    kube, desired, existing, _, reads = setup
    existing["spec"]["egress"] = [{}]
    kube.call.side_effect = PodgroveError("Conflict: resourceVersion changed")
    with pytest.raises(PodgroveError, match="Conflict"):
        kube.reconcile_network_policy([desired], IDENT)
    assert len(reads) == 2 and kube.call.call_count == 1


@pytest.mark.parametrize("kind", ["missing", "duplicate", "wrong-target", "wrong-label"])
def test_invalid_desired_policy_fails_without_cluster_access(setup, kind):
    kube, desired, _, _, reads = setup
    resources = [desired]
    if kind == "missing":
        resources.clear()
    elif kind == "duplicate":
        resources.append(deepcopy(desired))
    elif kind == "wrong-target":
        desired["metadata"]["namespace"] = "foreign"
    else:
        desired["metadata"]["labels"][ENVIRONMENT] = "foreign"
    with pytest.raises(PodgroveError):
        kube.reconcile_network_policy(resources, IDENT)
    assert reads == []
    kube.call.assert_not_called()
