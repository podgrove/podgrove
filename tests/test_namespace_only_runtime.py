"""A rejecting transport exercises lifecycle without cluster-scoped permission.

This is an API contract test, not a live admission or scheduling assertion.
"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from podgrove import kube as kube_module, reaper, state
from podgrove.kube import ENVIRONMENT, MANAGED, Kube, engine_pod_manifest, manifests

IDENT = "123456abcdef"
OTHER = "abcdef123456"
NAMESPACE = "arbitrary-existing-team"
CONTEXT = "selected-existing-credentials"


class NamespaceOnlyAPI:
    """Only this explicit namespaced resource vocabulary is available."""

    aliases = {
        "pod": "Pod", "pods": "Pod", "pods/exec": "Pod", "pods/portforward": "Pod",
        "statefulset": "StatefulSet", "statefulsets": "StatefulSet", "statefulsets.apps": "StatefulSet",
        "pvc": "PersistentVolumeClaim", "persistentvolumeclaim": "PersistentVolumeClaim",
        "persistentvolumeclaims": "PersistentVolumeClaim", "configmap": "ConfigMap", "configmaps": "ConfigMap",
        "service": "Service", "services": "Service", "networkpolicy": "NetworkPolicy",
        "networkpolicies": "NetworkPolicy", "networkpolicies.networking.k8s.io": "NetworkPolicy",
        "poddisruptionbudget": "PodDisruptionBudget", "poddisruptionbudgets": "PodDisruptionBudget",
        "poddisruptionbudgets.policy": "PodDisruptionBudget",
    }

    delete_paths = {
        "pods": "/api/v1", "persistentvolumeclaims": "/api/v1", "configmaps": "/api/v1",
        "services": "/api/v1", "statefulsets": "/apis/apps/v1",
        "networkpolicies": "/apis/networking.k8s.io/v1", "poddisruptionbudgets": "/apis/policy/v1",
    }

    def __init__(self, mode, *, ident=IDENT, namespace=NAMESPACE, context=CONTEXT):
        self.ident, self.namespace, self.context = ident, namespace, context
        self.calls = []
        self.objects = {}
        marker = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
            "name": "podgrove-bootstrap", "namespace": namespace,
            "labels": {MANAGED: "podgrove", "podgrove.dev/component": "bootstrap"}},
            "data": {"version": "1", "namespace_mode": mode}}
        if mode == "worktree":
            marker["data"]["environment"] = ident
        self.add(marker)
        self.add({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {
            "name": "podgrove-default-deny", "namespace": namespace,
            "labels": {MANAGED: "podgrove", "podgrove.dev/component": "bootstrap"}},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []}})
        self.add({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {
            "name": "pg-" + OTHER, "namespace": namespace,
            "labels": {MANAGED: "podgrove", ENVIRONMENT: OTHER}}})

    def add(self, item):
        item = deepcopy(item)
        item["metadata"].setdefault("uid", item["kind"] + "-" + item["metadata"]["name"])
        item["metadata"].setdefault("resourceVersion", "1")
        self.objects[item["kind"], item["metadata"]["name"]] = item
        return item

    @classmethod
    def kind(cls, value):
        assert value.lower() in cls.aliases, f"Forbidden or unreviewed API resource: {value}"
        return cls.aliases[value.lower()]

    def __call__(self, command, **kwargs):
        assert command[:6] == ["kubectl", "--context", self.context, "--namespace", self.namespace,
                               "--request-timeout=30s"]
        args = command[6:]
        assert "--all-namespaces" not in args and "-A" not in args
        assert not any(arg.startswith(("--context", "--namespace", "--as", "--kubeconfig")) for arg in args)
        assert "--raw" not in args or args[0] == "delete"
        self.calls.append((args, kwargs))
        if args[0] == "auth":
            assert args[:2] == ["auth", "can-i"] and len(args) == 4
            self.kind(args[3])
            return SimpleNamespace(returncode=0, stdout="yes", stderr="")
        if args[0] == "get":
            kinds = {self.kind(part) for part in args[1].split(",")}
            if len(args) > 2 and not args[2].startswith("-"):
                assert len(kinds) == 1
                kind, = kinds
                value = self.objects.get((kind, args[2]), {})
            else:
                wanted = dict(pair.split("=", 1) for pair in args[args.index("-l") + 1].split(",")) if "-l" in args else {}
                value = {"items": [item for (key, _), item in self.objects.items() if key in kinds and all(
                    item["metadata"].get("labels", {}).get(label) == val for label, val in wanted.items())]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")
        if args[0] in ("create", "replace"):
            item = json.loads(kwargs["input"])
            self.kind(item["kind"])
            assert item["metadata"]["namespace"] == self.namespace
            if "--dry-run=server" not in args:
                assert item["metadata"]["labels"].get(ENVIRONMENT) == self.ident
                self.add(item)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        assert args[0] == "delete", f"Unexpected operation: {args}"
        assert len(args) == 6 and args[:2] == ["delete", "--raw"] and args[3:5] == ["-f", "-"]
        assert args[-1].startswith("--request-timeout=") and kwargs["timeout"] > 0
        targets = [(plural, args[2].removeprefix(prefix)) for plural, base in self.delete_paths.items()
                   if args[2].startswith(prefix := f"{base}/namespaces/{self.namespace}/{plural}/")]
        assert len(targets) == 1, "Raw deletion must target an allowed resource in this exact namespace"
        plural, name = targets[0]
        assert name and "/" not in name and "?" not in name
        key = self.kind(plural), name
        item = self.objects[key]
        metadata = item["metadata"]
        assert metadata["namespace"] == self.namespace
        assert metadata["labels"].get(MANAGED) == "podgrove"
        assert metadata["labels"].get(ENVIRONMENT) == self.ident
        expected = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {
            "uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]}}
        if key[0] == "StatefulSet":
            expected["propagationPolicy"] = "Foreground"
        assert json.loads(kwargs["input"]) == expected
        if key[0] in ("PersistentVolumeClaim", "ConfigMap"):
            assert not any(kind in ("StatefulSet", "Pod") and value["metadata"].get("labels", {}).get(
                ENVIRONMENT) == self.ident for (kind, _), value in self.objects.items())
        del self.objects[key]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"kind": "Status", "status": "Success"}), stderr="")


@pytest.mark.parametrize("mode", ["shared", "worktree"])
@pytest.mark.parametrize("placement", ["shared", "tainted"])
@pytest.mark.parametrize("cleanup", ["down", "reap"])
def test_lifecycle_completes_with_strict_namespace_only_transport(monkeypatch, mode, placement, cleanup):
    api = NamespaceOnlyAPI(mode)
    monkeypatch.setattr(kube_module, "run", api)
    monkeypatch.setattr(state, "list_states", lambda *_: [])
    kube = Kube(CONTEXT, NAMESPACE, namespace_mode=mode)
    resources = manifests(NAMESPACE, IDENT, Path("/worktree/test-public-source"), "small", 1,
                          storage_class="admin-approved", namespace_mode=mode, node_mode=placement)
    retained = deepcopy(api.objects)
    kube.preflight(node_mode=placement)
    assert kube.ensure_namespace(IDENT) is False
    assert kube.check_storage(resources)[0]["reclaim_policy_verified"] is False
    kube.create_environment(resources, IDENT)
    controller = api.objects["StatefulSet", "pg-" + IDENT]
    pod = engine_pod_manifest(controller)
    pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
    api.add(pod)
    pvc = api.objects["PersistentVolumeClaim", "pg-" + IDENT]
    pvc["status"] = {"phase": "Bound"}
    pvc["spec"]["volumeName"] = "this-cluster-resource-must-never-be-read"
    kube.wait(IDENT, 1)
    kube.reconcile_network_policy(resources, IDENT)
    kube.heartbeat(IDENT, 1)
    assert kube.check_storage(resources)[0] == {"pvc": "pg-" + IDENT, "storage_class": "admin-approved",
        "phase": "Bound", "reclaim_policy": None, "reclaim_policy_verified": False}
    if cleanup == "reap":
        assert reaper.reap(kube, identity=IDENT)[0]["deleted"] is True
    else:
        kube.destroy(IDENT)
    assert api.objects == retained  # Includes marker, baseline policy and other worktree PVC.
    kube.destroy(IDENT)  # Missing lease/resources does not require Namespace access.
    assert api.objects == retained


def test_legacy_cleanup_needs_no_marker_namespace_or_cluster_permissions(monkeypatch):
    api = NamespaceOnlyAPI("shared")
    api.objects.pop(("ConfigMap", "podgrove-bootstrap"))
    monkeypatch.setattr(kube_module, "run", api)
    kube = Kube(CONTEXT, NAMESPACE, namespace_mode="exclusive")
    before = deepcopy(api.objects)
    kube.destroy(IDENT)
    assert api.objects == before
    assert [args[0] for args, _ in api.calls] == ["get", "get"]
