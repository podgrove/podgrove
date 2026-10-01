"""Offline CLI workflow: generated bootstrap, two worktrees, retained platform.

The public parser/dispatcher and Kube lifecycle methods run unchanged. Only
subprocess transport, Compose normalization and the background supervisor are
replaced. This models declared RBAC and object identity, not Kubernetes admission,
the scheduler, CSI reclamation or Docker behavior.
"""
from copy import deepcopy
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from podgrove import cli, runtime, state
from podgrove.compose import Compose
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube, engine_pod_manifest, resolve_namespace


CONTEXT = "offline:namespace-workflow"
GROUP = "workflow-developers"
STORAGE = "workflow-dynamic"
RESOURCES = {
    "StatefulSet": ("statefulsets", "apps"), "Pod": ("pods", ""),
    "PersistentVolumeClaim": ("persistentvolumeclaims", ""), "ConfigMap": ("configmaps", ""),
    "NetworkPolicy": ("networkpolicies", "networking.k8s.io"), "Service": ("services", ""),
    "PodDisruptionBudget": ("poddisruptionbudgets", "policy"),
}
CLUSTER_KINDS = {"Namespace", "StorageClass", "ClusterRole", "ClusterRoleBinding",
                 "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}
ALIASES = {alias: kind for kind, (plural, group) in RESOURCES.items()
           for alias in (kind.lower(), plural, plural + ("." + group if group else ""))}
ALIASES["pvc"] = "PersistentVolumeClaim"


class MemoryCluster:
    """Small transport adapter; all lifecycle decisions remain in real Kube."""

    def __init__(self):
        self.objects, self.calls, self.mutations = {}, [], []
        self.namespaces = set()  # Supplied by platform administration, never an API object here.
        self.admin = False

    @staticmethod
    def key(body):
        metadata = body["metadata"]
        return body["kind"], metadata.get("namespace"), metadata["name"]

    def add(self, body):
        body = deepcopy(body)
        assert body["kind"] not in CLUSTER_KINDS
        key = self.key(body)
        if key in self.objects:
            existing = deepcopy(self.objects[key])
            for field in ("uid", "resourceVersion"):
                existing["metadata"].pop(field, None)
            assert existing == body, "Shared bootstrap must not silently change platform objects"
            return
        body["metadata"].update(uid=f"offline-uid-{len(self.objects) + 1}", resourceVersion="1")
        self.objects[key] = body

    def allowed(self, namespace, verb, resource, group="", name=None):
        if self.admin:
            return True
        for binding in list(self.objects.values()):
            if binding["kind"] != "RoleBinding":
                continue
            if binding["metadata"]["namespace"] != namespace:
                continue
            if not any(subject.get("kind") == "Group" and subject.get("name") == GROUP
                       for subject in binding.get("subjects", [])):
                continue
            reference = binding["roleRef"]
            assert reference["kind"] == "Role"
            role = self.objects.get((reference["kind"], namespace,
                                     reference["name"]), {})
            if any(verb in rule["verbs"] and resource in rule["resources"] and group in rule["apiGroups"]
                   and ("resourceNames" not in rule or name in rule["resourceNames"])
                   for rule in role.get("rules", [])):
                return True
        return False

    def transport(self, kube, *args, **kwargs):
        assert kube.context == CONTEXT
        self.calls.append((kube.namespace, args))
        verb = args[0]
        assert "--all-namespaces" not in args and "-A" not in args
        assert not any(arg.startswith(("--context", "--namespace", "--as", "--kubeconfig", "--raw")) for arg in args)
        def result(value="", code=0):
            return SimpleNamespace(stdout=value, stderr="", returncode=code)
        if verb == "auth":
            assert args[1] == "can-i"
            resource, _, group = args[3].partition(".")
            assert resource in {"pods", "pods/exec", "pods/portforward", "statefulsets", "services", "persistentvolumeclaims", "networkpolicies", "configmaps", "poddisruptionbudgets"}
            permitted = self.allowed(kube.namespace, args[2], resource, group)
            return result("yes\n" if permitted else "no\n", 0 if permitted else 1)
        if verb == "get":
            assert args[1].lower() in ALIASES, "No cluster-scoped or unreviewed resource reads"
            kind = ALIASES[args[1].lower()]
            name = args[2]
            resource, group = RESOURCES[kind]
            if name == "-l":
                selector = {MANAGED: "podgrove", "podgrove.dev/component": "connection"}
                assert kind == "NetworkPolicy" and args[2:] == (
                    "-l", f"{MANAGED}=podgrove,podgrove.dev/component=connection", "-o", "json")
                assert self.allowed(kube.namespace, "list", resource, group), "Missing generated LIST grant: NetworkPolicy"
                items = [body for (current_kind, namespace, _), body in self.objects.items()
                         if current_kind == kind and namespace == kube.namespace
                         and all(body["metadata"].get("labels", {}).get(key) == value for key, value in selector.items())]
                return result(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
            assert not name.startswith("-")
            assert self.allowed(kube.namespace, "get", resource, group, name), f"Missing generated GET grant: {kind}/{name}"
            body = self.objects.get((kind, kube.namespace, name))
            return result(json.dumps(body) if body else "")
        if verb == "create":
            body = json.loads(kwargs["input"])
            assert body["kind"] not in CLUSTER_KINDS, "Runtime must not create platform objects"
            assert body["metadata"]["namespace"] == kube.namespace
            assert kube.namespace in self.namespaces
            resource, group = RESOURCES[body["kind"]]
            assert self.allowed(kube.namespace, "create", resource, group)
            if "--dry-run=server" in args:
                assert body["kind"] in ("Pod", "PodDisruptionBudget")
                return result(json.dumps(body))
            assert self.key(body) not in self.objects
            self.mutations.append((kube.namespace, "create", body["kind"], body["metadata"]["name"]))
            self.add(body)
            if body["kind"] == "StatefulSet":
                self.add(engine_pod_manifest(self.objects[self.key(body)]))
            return result()
        assert verb == "delete", f"Unexpected transport operation {args}"
        assert "-l" in args and "namespace" not in args, "Normal cleanup must retain platform namespaces"
        selector = dict(part.split("=", 1) for part in args[args.index("-l") + 1].split(","))
        assert set(selector) == {MANAGED, ENVIRONMENT} and selector[MANAGED] == "podgrove"
        kinds = {ALIASES[alias] for alias in args[1].split(",")}
        for kind in kinds:
            assert kind not in CLUSTER_KINDS
            resource, group = RESOURCES[kind]
            assert self.allowed(kube.namespace, "delete", resource, group)
        doomed = [key for key, body in self.objects.items()
                  if key[0] in kinds and key[1] == kube.namespace
                  and all(body["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())]
        for key in doomed:
            body = self.objects.pop(key)
            self.mutations.append((kube.namespace, "delete", key[0], key[2]))
            if key[0] == "StatefulSet":
                assert "--cascade=foreground" in args
                for child, pod in list(self.objects.items()):
                    if child[0] == "Pod" and child[1] == kube.namespace and any(
                        owner.get("uid") == body["metadata"]["uid"] for owner in pod["metadata"].get("ownerReferences", [])
                    ):
                        del self.objects[child]
        return result()


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    cluster = MemoryCluster()
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "private-state"))
    monkeypatch.delenv("PODGROVE_CONTEXT", raising=False)

    class OfflineKube(Kube):
        def call(self, *args, **kwargs):
            return cluster.transport(self, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Offline workflow must not start processes or network connections")

    def ready(path):
        data = state.read(path)
        data.update(status="ready", ports=[], services=[])
        state.write(path, data)

    monkeypatch.setattr(cli, "Kube", OfflineKube)
    monkeypatch.setattr(Compose, "model", lambda self: {"services": {"app": {"image": "offline/app:fixture"}}})
    monkeypatch.setattr(runtime, "spawn", ready)
    monkeypatch.setattr(runtime, "stop_session", lambda data: None)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    return cluster


def command(root, name, *extra):
    return cli.execute(cli.parser().parse_args([name, "--project-directory", str(root), *extra]))


def project(tmp_path, name, mode):
    root = tmp_path / name
    root.mkdir()
    (root / "compose.yaml").write_text("services:\n  app:\n    image: offline/app:fixture\n")
    (root / "podgrove.yml").write_text(yaml.safe_dump({"cluster": {
        "context": CONTEXT, "namespace": "team-development", "namespace_mode": mode, "storage_class": STORAGE,
    }}))
    return root


def install(root, cluster, output):
    assert command(root, "bootstrap", "--output", str(output), "--developer-group", GROUP) == 0
    for path in sorted(output.glob("*.yaml")):
        for body in yaml.safe_load_all(path.read_text()):
            assert body["kind"] not in CLUSTER_KINDS
            cluster.namespaces.add(body["metadata"]["namespace"])  # Namespace already provisioned externally.
            cluster.add(body)  # Namespaced administrator installation, entirely in memory.


@pytest.mark.parametrize("mode", ["shared", "worktree"])
@pytest.mark.parametrize("lose_state", [False, True], ids=["recorded-cleanup", "lease-recovery"])
def test_two_worktrees_bootstrap_up_down_retain_platform_and_other_worktree(tmp_path, workflow, mode, lose_state):
    first, second = [project(tmp_path, name, mode) for name in ("first", "second")]
    for root in (first, second):
        install(root, workflow, tmp_path / (root.name + "-bootstrap"))
    platform = deepcopy(workflow.objects)
    records = []
    for root in (first, second):
        assert command(root, "up", "--json") == 0
        data = state.read(state.state_path(root, CONTEXT))
        assert data["namespace"] == resolve_namespace("team-development", mode, state.identity(root))
        assert data["namespace_mode"] == mode
        assert workflow.objects[("ConfigMap", data["namespace"], "pg-" + data["identity"])]["data"]["namespace_mode"] == mode
        assert workflow.objects[("PersistentVolumeClaim", data["namespace"], "pg-" + data["identity"])]["spec"]["storageClassName"] == STORAGE
        records.append(data)
    assert (records[0]["namespace"] == records[1]["namespace"]) is (mode == "shared")
    # A matching environment label alone must never select a foreign resource.
    foreign = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": "unrelated-platform-data", "namespace": records[0]["namespace"],
        "labels": {MANAGED: "another-owner", ENVIRONMENT: records[0]["identity"]}}, "data": {"keep": "yes"}}
    workflow.add(foreign)
    before = deepcopy(workflow.objects)
    first_path = state.state_path(first, CONTEXT)
    if lose_state:
        first_path.unlink()  # Simulate lost local metadata; recover mode from owned cluster lease.
    # An exact namespace flag initially selects shared mode. The worktree case
    # therefore succeeds only if down recovers its mode from the owned lease.
    recovery_target = ("--namespace", records[0]["namespace"]) if lose_state else ()
    assert command(first, "down", *recovery_target) == 0
    assert not first_path.exists()
    assert state.read(state.state_path(second, CONTEXT)) == records[1]
    first_owned = {key for key, body in before.items() if key[1] == records[0]["namespace"]
                   and body["metadata"].get("labels", {}).get(MANAGED) == "podgrove"
                   and body["metadata"].get("labels", {}).get(ENVIRONMENT) == records[0]["identity"]}
    assert workflow.objects == {key: body for key, body in before.items() if key not in first_owned}
    assert {key: workflow.objects[key] for key in platform} == platform
    assert command(second, "down") == 0
    assert workflow.objects == {**platform, MemoryCluster.key(foreign): before[MemoryCluster.key(foreign)]}
    assert not list(state.state_home(create=False).glob("*"))
    deleted_kinds = {kind for _, verb, kind, _ in workflow.mutations if verb == "delete"}
    assert deleted_kinds == {"StatefulSet", "PersistentVolumeClaim", "ConfigMap", "NetworkPolicy", "Service", "PodDisruptionBudget"}
    assert any("--dry-run=server" in args for _, args in workflow.calls)


@pytest.mark.parametrize("mode", ["shared", "worktree"])
def test_missing_bootstrap_marker_fails_before_any_resource_creation(tmp_path, workflow, mode):
    root = project(tmp_path, "missing-bootstrap", mode)
    workflow.admin = True  # Existing permissions never substitute for the bootstrap marker.
    with pytest.raises(PodgroveError, match="Bootstrap ConfigMap .* is missing"):
        command(root, "up")
    assert workflow.mutations == []
    assert not any(args[0] == "create" for _, args in workflow.calls)
    assert not state.state_path(root, CONTEXT).exists()


def test_worktree_bootstrap_for_another_identity_is_refused_before_create(tmp_path, workflow):
    root = project(tmp_path, "wrong-owner", "worktree")
    install(root, workflow, tmp_path / "wrong-owner-bootstrap")
    namespace = resolve_namespace("team-development", "worktree", state.identity(root))
    workflow.objects[("ConfigMap", namespace, "podgrove-bootstrap")]["data"]["environment"] = "012345abcdef"
    before = deepcopy(workflow.objects)
    with pytest.raises(PodgroveError, match="marker identity"):
        command(root, "up")
    assert workflow.objects == before and workflow.mutations == []
    assert not any(args[0] == "create" for _, args in workflow.calls)
    assert not state.state_path(root, CONTEXT).exists()
