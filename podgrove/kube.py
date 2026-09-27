"""Explicit-context Kubernetes operations and ownership fences."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import PodgroveError
from .network import policy_spec
from .bootstrap import PROVISIONING_MARKER
from .process import run
from .repository import repository_labels
from .resources import engine_resources, initializer_resources, quantity_text, same_resources

MANAGED = "app.kubernetes.io/managed-by"
ENVIRONMENT = "podgrove.dev/environment"
DEDICATED = "podgrove.dev/dedicated"
NODE_MODE = "podgrove.dev/node-mode"
IMAGE = "docker:29.5.2-dind"
REQUEST_TIMEOUT = 30
# Allow kubectl to finish its request and authentication/cleanup work before
# the local subprocess deadline expires.
REQUEST_PROCESS_TIMEOUT = REQUEST_TIMEOUT + 5

DEFAULT_TAINTED_NODES = {"selector": {DEDICATED: "true"},
                         "taint": {"key": "dedicated", "value": "podgrove", "effect": "NoSchedule"}}


def context_name(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PodgroveError(
            "Set cluster.context in podgrove.yml, --context, or PODGROVE_CONTEXT; "
            "the current kube context is never used"
        )
    if len(value) > 512 or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PodgroveError("Invalid cluster context: use at most 512 characters without control characters")
    return value


def validate_tainted_nodes(value: dict) -> None:
    def label_key(key):
        if not isinstance(key, str):
            return False
        parts = key.split("/")
        name = parts[-1]
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?", name):
            return False
        if len(parts) == 1:
            return True
        prefix = parts[0]
        return len(parts) == 2 and len(prefix) <= 253 and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in prefix.split("."))

    def label_value(item):
        return isinstance(item, str) and (item == "" or bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?", item)))

    if not isinstance(value, dict) or set(value) - {"selector", "taint"}:
        raise PodgroveError("tainted_nodes: expected only selector and taint")
    selector, taint = value.get("selector"), value.get("taint")
    if not isinstance(selector, dict) or not selector:
        raise PodgroveError("tainted_nodes.selector: requires at least one node label")
    for key, item in selector.items():
        if not label_key(key) or not label_value(item):
            raise PodgroveError(f"tainted_nodes.selector: invalid Kubernetes label {key!r}={item!r}")
    if selector.get("kubernetes.io/os", "linux") != "linux":
        raise PodgroveError("tainted_nodes.selector.kubernetes.io/os: only Linux Docker engines are supported")
    if selector.get("eks.amazonaws.com/compute-type") in ("fargate", "auto"):
        raise PodgroveError(
            "tainted_nodes.selector.eks.amazonaws.com/compute-type: Fargate and EKS Auto Mode "
            "cannot run the privileged Docker engine"
        )
    if not isinstance(taint, dict) or set(taint) - {"key", "value", "effect"}:
        raise PodgroveError("tainted_nodes.taint: expected only key, value and effect")
    if not label_key(taint.get("key")):
        raise PodgroveError("tainted_nodes.taint.key: invalid Kubernetes qualified name")
    if not label_value(taint.get("value")):
        raise PodgroveError("tainted_nodes.taint.value: invalid Kubernetes label value")
    if taint.get("effect") not in ("NoSchedule", "NoExecute"):
        raise PodgroveError("tainted_nodes.taint.effect: must be NoSchedule or NoExecute")


def tainted_placement(value: dict | None = None) -> dict:
    value = value if value is not None else DEFAULT_TAINTED_NODES
    validate_tainted_nodes(value)
    return {"selector": dict(value["selector"]), "taint": dict(value["taint"])}


def shared_namespace(value: str) -> bool:
    """Legacy inference only; explicit selection must supply its lifecycle mode."""
    return resolve_namespace_mode(value) == "shared"


def namespace_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise PodgroveError("namespace must be a Kubernetes DNS label: 1–63 lowercase letters, digits or hyphens, with alphanumeric ends")
    return value


def resolve_namespace_mode(namespace: str, mode: str | None = None) -> str:
    """Validate ownership mode; infer missing values only for legacy records."""
    namespace_name(namespace)
    if mode is None:
        return "exclusive" if namespace.startswith("wt-") else "shared"
    if mode not in ("shared", "worktree", "exclusive"):
        raise PodgroveError("namespace_mode must be shared, worktree or legacy exclusive")
    return mode


def resolve_namespace(base: str, mode: str, ident: str) -> str:
    """Choose a configured shared namespace or deterministic bootstrap target."""
    namespace_name(base)
    if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{12}", ident):
        raise PodgroveError("Invalid environment identity")
    if mode == "shared":
        return base
    if mode != "worktree":
        raise PodgroveError("New namespace_mode must be shared or worktree")
    if len(base) > 47:
        base = base[:38].rstrip("-") + "-" + hashlib.sha256(base.encode()).hexdigest()[:8]
    return namespace_name(f"{base}-wt-{ident}")


def engine_pod_name(ident: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{12}", ident):
        raise PodgroveError("Invalid environment identity")
    return f"pg-{ident}-0"


def engine_pod_manifest(controller: dict) -> dict:
    """Render the StatefulSet's first Pod for admission dry-run, without a fake owner."""
    metadata = controller["metadata"]
    ident = metadata["labels"][ENVIRONMENT]
    template = copy.deepcopy(controller["spec"]["template"])
    template["metadata"].update(name=engine_pod_name(ident), namespace=metadata["namespace"])
    template["metadata"].setdefault("labels", {}).update({
        "statefulset.kubernetes.io/pod-name": engine_pod_name(ident), "apps.kubernetes.io/pod-index": "0",
    })
    template["spec"].update(hostname=engine_pod_name(ident), subdomain=controller["spec"]["serviceName"])
    if metadata.get("uid"):
        template["metadata"]["ownerReferences"] = [{
            "apiVersion": "apps/v1", "kind": "StatefulSet", "name": metadata["name"],
            "uid": metadata["uid"], "controller": True, "blockOwnerDeletion": True,
        }]
    return {"apiVersion": "v1", "kind": "Pod", **template}


def manifests(namespace: str, ident: str, root: Path, size: str, ttl: int,
              *, storage_class: str | None = None, storage: str = "20Gi", mr_url: str = "",
              node_mode: str = "shared", tainted_nodes: dict | None = None,
              namespace_mode: str | None = None, network: dict | None = None,
              resources: dict | None = None, init_resources: dict | None = None) -> list[dict]:
    namespace_name(namespace)
    namespace_mode = resolve_namespace_mode(namespace, namespace_mode)
    if node_mode not in ("shared", "tainted"):
        raise PodgroveError("node_mode must be shared or tainted")
    placement = tainted_placement(tainted_nodes)
    if not re.fullmatch(r"[a-f0-9]{12}", ident):
        raise PodgroveError("Invalid environment identity")
    if len(root.parts) < 3 or ":" in str(root) or "\n" in str(root) or any(root == Path(p) or Path(p) in root.parents
                               for p in ("/proc", "/sys", "/dev", "/var/lib/docker", "/run", "/etc", "/bin", "/usr",
                                         "/sbin", "/lib", "/boot", "/var/run", "/private/etc", "/private/var/run")):
        raise PodgroveError(f"Unsafe worktree mirror path: {root}")
    def label_value(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "-", value).strip("-_.")[:63].rstrip("-_.") or "unspecified"
    repository = repository_labels(root)
    labels = {MANAGED: "podgrove", ENVIRONMENT: ident, NODE_MODE: node_mode, "podgrove.dev/worktree": ident,
              "podgrove.dev/repo": label_value(repository["repo"]),
              "podgrove.dev/owner": label_value(os.environ.get("PODGROVE_OWNER", os.environ.get("USER", "unknown"))),
              "podgrove.dev/branch": label_value(repository["branch"])}
    name = f"pg-{ident}"
    def meta(n=name):
        return {"name": n, "namespace": namespace, "labels": labels.copy()}
    budget = engine_resources(size, resources)
    init_budget = initializer_resources(init_resources)
    storage = quantity_text(storage, "storage.size")
    pvc_spec = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": storage}}}
    if storage_class:
        pvc_spec["storageClassName"] = storage_class
    pod = {
        "apiVersion": "v1", "kind": "Pod", "metadata": meta(),
        "spec": {
            "automountServiceAccountToken": False, "enableServiceLinks": False,
            "nodeSelector": {"kubernetes.io/os": "linux", **(placement["selector"] if node_mode == "tainted" else {})},
            "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{
                "matchExpressions": [{"key": "eks.amazonaws.com/compute-type", "operator": "NotIn", "values": ["fargate", "auto"]}]
            }]}}},
            "tolerations": ([{"operator": "Equal", **placement["taint"]}]
                            if node_mode == "tainted" else []),
            "terminationGracePeriodSeconds": 90,
            "initContainers": [{"name": "storage", "image": "alpine:3.21",
                "command": ["sh", "-ec", "mkdir -p /data/docker /data/worktree"],
                "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                "resources": init_budget,
                "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
            "containers": [{"name": "docker", "image": IMAGE,
                "command": ["dockerd-entrypoint.sh"],
                "args": ["dockerd", "--host=tcp://127.0.0.1:2375", "--host=unix:///var/run/docker.sock", "--tls=false"],
                "env": [{"name": "DOCKER_TLS_CERTDIR", "value": ""},
                        {"name": "PODGROVE_POD_UID", "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}}}],
                "securityContext": {"privileged": True},
                "resources": budget,
                "readinessProbe": {"exec": {"command": ["docker", "-H", "unix:///var/run/docker.sock", "info"]},
                                   "periodSeconds": 3, "timeoutSeconds": 3, "failureThreshold": 20},
                "volumeMounts": [{"name": "data", "mountPath": "/var/lib/docker", "subPath": "docker"},
                                 {"name": "data", "mountPath": str(root), "subPath": "worktree"}]}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": name}}],
        },
    }
    # Docker API listens only on loopback; kubectl tunnels reach it via kubelet.
    policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta(),
              "spec": policy_spec(ident, network)}
    lease = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta(),
             "data": {"root": str(root), "last_activity": str(time.time()), "ttl_seconds": str(ttl),
                      "mr_url": mr_url, "namespace_mode": namespace_mode}}
    selector = {MANAGED: "podgrove", ENVIRONMENT: ident}
    # The Service supplies stable StatefulSet identity only. No Docker API port
    # is exposed: the daemon remains bound to the Pod's loopback interface.
    service = {"apiVersion": "v1", "kind": "Service", "metadata": meta(),
               "spec": {"clusterIP": "None", "selector": selector.copy()}}
    controller = {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": meta(),
                  "spec": {"replicas": 1, "serviceName": name, "selector": {"matchLabels": selector.copy()},
                           "updateStrategy": {"type": "OnDelete"},
                           "template": {"metadata": {"labels": labels.copy()}, "spec": pod["spec"]}}}
    return [policy, {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": meta(), "spec": pvc_spec},
            lease, service, controller]


class Kube:
    def __init__(self, context: str, namespace: str, *, namespace_mode: str | None = None):
        self.context = context_name(context)
        self.namespace = namespace_name(namespace)
        self.namespace_mode = resolve_namespace_mode(namespace, namespace_mode)

    def command(self, *args: str) -> list[str]:
        return ["kubectl", "--context", self.context, "--namespace", self.namespace,
                f"--request-timeout={REQUEST_TIMEOUT}s", *args]

    def call(self, *args: str, **kwargs):
        return run(self.command(*args), **kwargs)

    def get(self, kind: str, name: str | None = None, *, selector: str | None = None) -> dict:
        args = ["get", kind]
        if name:
            args.append(name)
        if selector:
            args += ["-l", selector]
        args += ["-o", "json", "--ignore-not-found"]
        result = self.call(*args)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def preflight(self, node_mode: str = "shared", tainted_nodes: dict | None = None) -> None:
        if node_mode not in ("shared", "tainted"):
            raise PodgroveError("node_mode must be shared or tainted")
        tainted_placement(tainted_nodes)
        # Both placement modes rely on the scheduler. Node inventory is outside
        # the namespace-scoped runtime identity; Pod status reports scheduling.
        for resource, verb in (("pods", "create"), ("pods/portforward", "create"), ("pods/portforward", "get"),
                               ("pods/exec", "create"), ("pods/exec", "get"), ("statefulsets.apps", "create"),
                               ("statefulsets.apps", "delete"), ("services", "create"), ("persistentvolumeclaims", "create"),
                               ("networkpolicies.networking.k8s.io", "create"), ("configmaps", "create")):
            result = self.call("auth", "can-i", verb, resource, check=False)
            if result.returncode or result.stdout.strip() != "yes":
                raise PodgroveError(f"Kubernetes access missing: {verb} {resource} in {self.namespace}")

    def ensure_namespace(self, ident: str, environment_labels: dict | None = None) -> bool:
        """Verify the namespaced bootstrap marker without inspecting Namespace.

        The marker belongs to the namespace administrator and is retained by
        cleanup. A missing marker never triggers namespace creation or adoption.
        """
        if not re.fullmatch(r"[a-f0-9]{12}", ident):
            raise PodgroveError("Invalid environment identity")
        if self.namespace_mode == "exclusive":
            raise PodgroveError("Legacy exclusive environments must be cleaned with down before installing a shared or worktree bootstrap")
        marker = self.get("configmap", PROVISIONING_MARKER)
        guidance = (f"Regenerate the namespaced bootstrap for {self.namespace} and have its administrator "
                    "apply the reviewed bundle using the selected context; Podgrove never creates namespaces")
        if not marker:
            raise PodgroveError(f"Bootstrap ConfigMap {self.namespace}/{PROVISIONING_MARKER} is missing. {guidance}")
        metadata, data = marker.get("metadata", {}), marker.get("data", {})
        labels = metadata.get("labels", {})
        if (metadata.get("name") != PROVISIONING_MARKER or metadata.get("namespace") != self.namespace
                or metadata.get("deletionTimestamp") or labels.get(MANAGED) != "podgrove"
                or labels.get("podgrove.dev/component") != "bootstrap" or ENVIRONMENT in labels
                or not isinstance(data, dict) or data.get("version") != "1"
                or data.get("namespace_mode") != self.namespace_mode
                or (self.namespace_mode == "worktree" and data.get("environment") != ident)
                or (self.namespace_mode == "shared" and "environment" in data)):
            raise PodgroveError(f"Bootstrap marker identity, version or namespace_mode does not match this environment. {guidance}")
        return False

    def lease_mode(self, ident: str, lease: dict | None = None) -> str:
        """Recover lifecycle mode from an exact owned lease, without mutations."""
        if not re.fullmatch(r"[a-f0-9]{12}", ident):
            raise PodgroveError("Invalid environment identity")
        if lease is None:
            lease = self.get("configmap", f"pg-{ident}")
        if not lease:
            return self.namespace_mode
        metadata = lease.get("metadata", {})
        labels = metadata.get("labels", {})
        if (metadata.get("name") != f"pg-{ident}" or metadata.get("namespace") != self.namespace
                or labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != ident):
            raise PodgroveError("Environment lease is foreign; refusing lifecycle changes")
        data = lease.get("data", {})
        if not isinstance(data, dict):
            raise PodgroveError("Invalid environment lease lifecycle metadata")
        if "namespace_mode" in data and data["namespace_mode"] is None:
            raise PodgroveError("Invalid environment lease namespace_mode")
        return resolve_namespace_mode(self.namespace, data.get("namespace_mode"))

    def check_storage(self, resources: list[dict]) -> list[dict]:
        """Validate owned PVCs; trust the administrator's explicit storage class.

        Namespace-scoped credentials cannot inspect reclaim policy, provisioners
        or backing volumes. PVC binding is never reported as proof of deletion.
        An existing owned PVC can supply its already recorded class on reconnect.
        """
        checked = []
        for resource in resources:
            if resource.get("kind") != "PersistentVolumeClaim":
                continue
            metadata, spec = resource.get("metadata", {}), resource.get("spec", {})
            labels = metadata.get("labels", {})
            ident = labels.get(ENVIRONMENT)
            if (metadata.get("namespace") != self.namespace or not isinstance(ident, str)
                    or not re.fullmatch(r"[a-f0-9]{12}", ident) or metadata.get("name") != f"pg-{ident}"
                    or labels.get(MANAGED) != "podgrove"):
                raise PodgroveError("Storage preflight requires an exact namespaced Podgrove PVC identity")
            existing = self.get("persistentvolumeclaim", metadata["name"])
            if existing:
                actual = existing.get("metadata", {})
                if actual.get("namespace") != self.namespace or actual.get("name") != metadata["name"]:
                    raise PodgroveError("Existing PVC identity differs from the requested environment")
                self._validate_existing(resource, existing, ident)
            name = spec.get("storageClassName", existing.get("spec", {}).get("storageClassName"))
            if (not isinstance(name, str) or not name or len(name) > 253
                    or any(len(part) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", part)
                           for part in name.split("."))):
                raise PodgroveError("Storage preflight requires an explicit administrator-approved storage class: "
                                    "set cluster.storage_class or --storage-class. Podgrove cannot inspect "
                                    "StorageClasses or verify reclaimPolicy Delete with namespace-scoped access")
            spec["storageClassName"] = name
            checked.append({"pvc": metadata["name"], "storage_class": name,
                            "phase": existing.get("status", {}).get("phase"),
                            "reclaim_policy": None, "reclaim_policy_verified": False})
        return checked

    def _validate_existing(self, resource: dict, existing: dict, ident: str) -> None:
        labels = existing["metadata"].get("labels", {})
        if labels.get(ENVIRONMENT) != ident or labels.get(MANAGED) != "podgrove":
            raise PodgroveError(f"Refusing to modify unowned {resource['kind']}/{resource['metadata']['name']}")
        if resource["kind"] == "ConfigMap":
            wanted = self.lease_mode(ident, resource)
            if self.lease_mode(ident, existing) != wanted or wanted != self.namespace_mode:
                raise PodgroveError("Existing environment has a different namespace_mode; run down before changing namespace ownership")
            return
        if resource["kind"] == "PersistentVolumeClaim":
            wanted, actual = resource["spec"], existing.get("spec", {})
            def quantity(value):
                match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))([EPTGMK]i|[EPTGMKkmun]|[eE][+-]?\d+)?", str(value))
                if not match:
                    return value
                number, suffix = match.groups()
                suffix = suffix or ""
                try:
                    if suffix.endswith("i"):
                        factor = Decimal(1024) ** ("KMGTPE".index(suffix[0]) + 1)
                    elif suffix.startswith(("e", "E")) and len(suffix) > 1:
                        factor = Decimal(10) ** int(suffix[1:])
                    else:
                        factor = Decimal(10) ** {"": 0, "n": -9, "u": -6, "m": -3, "k": 3, "K": 3,
                                                "M": 6, "G": 9, "T": 12, "P": 15, "E": 18}[suffix]
                    return Decimal(number) * factor
                except (InvalidOperation, ValueError, KeyError):
                    return value
            wanted_size = wanted.get("resources", {}).get("requests", {}).get("storage")
            actual_size = actual.get("resources", {}).get("requests", {}).get("storage")
            if (existing.get("metadata", {}).get("deletionTimestamp")
                    or quantity(wanted_size) != quantity(actual_size)
                    or set(wanted.get("accessModes", [])) != set(actual.get("accessModes", []))
                    or actual.get("volumeMode", "Filesystem") != wanted.get("volumeMode", "Filesystem")
                    or ("storageClassName" in wanted and actual.get("storageClassName") != wanted["storageClassName"])):
                raise PodgroveError(
                    "Existing PersistentVolumeClaim uses different storage settings or is being deleted; "
                    "run down before recreating it (down deletes environment data). Existing storage was left unchanged."
                )
            return
        if resource["kind"] == "Service":
            spec = existing.get("spec", {})
            if (spec.get("clusterIP") != "None" or spec.get("selector") != resource["spec"]["selector"]
                    or spec.get("type", "ClusterIP") != "ClusterIP" or spec.get("ports")):
                raise PodgroveError("Existing Service differs from the engine's headless identity service; run down before recreating it")
            return
        if resource["kind"] == "StatefulSet":
            # API defaulting adds fields and omits empty strings/lists. Compare
            # every declared field while accepting those representation changes.
            def matches(expected, actual):
                if isinstance(expected, dict):
                    return isinstance(actual, dict) and all(
                        (same_resources(value, actual.get(key, {})) if key == "resources" else
                         matches(value, actual.get(key, value if value in ("", [], {}) else None)))
                        for key, value in expected.items())
                if isinstance(expected, list):
                    return isinstance(actual, list) and len(expected) == len(actual) and all(
                        matches(left, right) for left, right in zip(expected, actual))
                return expected == actual
            if existing.get("metadata", {}).get("deletionTimestamp"):
                raise PodgroveError("Existing StatefulSet is being deleted; wait for cleanup before recreating the environment")
            expected_spec = copy.deepcopy(resource["spec"])
            # Repo/branch labels are creation-time advice, not engine settings.
            # A local checkout change must not force destructive recreation or
            # roll the existing controller merely to refresh these two labels.
            for key in ("podgrove.dev/repo", "podgrove.dev/branch"):
                expected_spec.get("template", {}).get("metadata", {}).get("labels", {}).pop(key, None)
            if not matches(expected_spec, existing.get("spec", {})):
                raise PodgroveError(
                    "Existing StatefulSet uses different engine settings (node_mode, tainted_nodes, resources, init_resources, image or storage); "
                    "live resizing is not supported. The existing controller was left unchanged. "
                    "Restore its settings or plan an explicit recreation after saving data; down deletes environment data."
                )
            return
        if resource["kind"] != "Pod":
            return
        actual_mode = labels.get(NODE_MODE)
        if not actual_mode:
            actual_mode = "tainted" if existing.get("spec", {}).get("nodeSelector", {}).get(DEDICATED) == "true" else "shared"
        actual_mode = "tainted" if actual_mode == "dedicated" else actual_mode
        if actual_mode != resource["metadata"]["labels"][NODE_MODE]:
            raise PodgroveError("Existing Pod uses a different node_mode; run down before recreating it with the new mode")
        if actual_mode == "tainted" and (
            existing["spec"].get("nodeSelector") != resource["spec"]["nodeSelector"]
            or not all(t in existing["spec"].get("tolerations", []) for t in resource["spec"]["tolerations"])
        ):
            raise PodgroveError("Existing Pod uses different tainted_nodes settings; run down before recreating it")

    @staticmethod
    def _validate_pod_controller(pod: dict, controller: dict, ident: str) -> None:
        metadata = pod.get("metadata", {})
        labels = metadata.get("labels", {})
        if labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != ident:
            raise PodgroveError(f"Pod {metadata.get('name', engine_pod_name(ident))} is no longer owned by this worktree")
        owner = controller.get("metadata", {})
        if not owner.get("uid") or not any(
            reference.get("apiVersion") == "apps/v1" and reference.get("kind") == "StatefulSet"
            and reference.get("name") == f"pg-{ident}" and reference.get("uid") == owner["uid"]
            and reference.get("controller") is True
            for reference in metadata.get("ownerReferences", [])
        ):
            raise PodgroveError(f"Pod {metadata.get('name', engine_pod_name(ident))} is not controlled by the owned StatefulSet")

    def check_engine_settings(self, resources: list[dict]) -> None:
        """Refuse incompatible running engines before policy or session changes."""
        for resource in resources:
            if resource["kind"] != "StatefulSet":
                continue
            metadata = resource["metadata"]
            existing = self.get("StatefulSet", metadata["name"])
            if not existing:
                raise PodgroveError("The running session's engine controller is missing; resources were left unchanged")
            self._validate_existing(resource, existing, metadata["labels"][ENVIRONMENT])

    def check_admission(self, resources: list[dict]) -> None:
        """Ask admission about new engines before provisioning supporting objects.

        The namespace must already exist. Server dry-run applies the real Pod
        admission policies without persisting a Pod or requiring its PVC first.
        Existing controller-owned engines need no new Pod admission for a reconnect.
        """
        for resource in resources:
            if resource["kind"] != "StatefulSet":
                continue
            metadata = resource["metadata"]
            ident = metadata["labels"][ENVIRONMENT]
            existing_controller = self.get("StatefulSet", metadata["name"])
            if existing_controller:
                self._validate_existing(resource, existing_controller, ident)
            legacy = self.get("pod", metadata["name"])
            if legacy:
                raise PodgroveError(
                    "A legacy bare engine Pod still exists; refusing to attach a second Docker engine to its PVC. "
                    "Run down before recreating the environment with a StatefulSet (down deletes environment data)."
                )
            pod = engine_pod_manifest(resource)
            existing = self.get("pod", pod["metadata"]["name"])
            if existing:
                self._validate_existing(pod, existing, ident)
                self._validate_pod_controller(existing, existing_controller, ident)
                continue
            if existing_controller:
                pod["metadata"]["ownerReferences"] = engine_pod_manifest(existing_controller)["metadata"]["ownerReferences"]
            try:
                self.call("create", "--dry-run=server", "-f", "-", input=json.dumps(pod))
            except PodgroveError as exc:
                # A healthy controller may create the stable Pod between our
                # GET and dry-run. Accept only its genuinely owned replacement.
                if existing_controller:
                    appeared = self.get("pod", pod["metadata"]["name"])
                    if appeared:
                        self._validate_existing(pod, appeared, ident)
                        self._validate_pod_controller(appeared, existing_controller, ident)
                        continue
                raise PodgroveError(
                    f"Kubernetes admission preflight failed for the required privileged Docker engine "
                    f"in namespace {self.namespace}. Both node_mode=shared and node_mode=tainted require "
                    "admission permission for privileged pods; changing node scheduling does not bypass "
                    f"admission policy. Server detail: {exc}"
                ) from exc

    def reconcile_network_policy(self, resources: list[dict], ident: str) -> None:
        """Restore this engine's exact allow rules, including on idempotent up.

        NetworkPolicies are additive: an extra allow rule must be removed, not
        accepted as an API-defaulted field. Only empty-rule/selector encoding
        differences are normalized before the complete spec comparison.
        """
        policies = [resource for resource in resources if resource.get("kind") == "NetworkPolicy"]
        if len(policies) != 1:
            raise PodgroveError("Expected exactly one environment NetworkPolicy")
        desired = copy.deepcopy(policies[0])
        metadata = desired.get("metadata", {})
        name = "pg-" + ident
        if metadata.get("name") != name or metadata.get("namespace") != self.namespace:
            raise PodgroveError("NetworkPolicy target does not match this environment")
        self._validate_existing(desired, desired, ident)
        self.ensure_namespace(ident)
        existing = self.get("NetworkPolicy", name)
        if not existing:
            self.call("create", "-f", "-", input=json.dumps(desired))
            return
        self._validate_existing(desired, existing, ident)
        observed = existing.get("metadata", {})
        if (observed.get("name") != name or observed.get("namespace") != self.namespace
                or not observed.get("uid") or not observed.get("resourceVersion")
                or observed.get("deletionTimestamp")):
            raise PodgroveError("Existing NetworkPolicy identity is incomplete, changed, or being deleted")

        def normalize(spec):
            normalized = copy.deepcopy(spec)
            if not isinstance(normalized, dict):
                return normalized
            for direction in ("ingress", "egress"):
                if normalized.get(direction) is None:
                    normalized[direction] = []

            def selectors(value):
                if isinstance(value, list):
                    for item in value:
                        selectors(item)
                elif isinstance(value, dict):
                    for key, child in value.items():
                        if key in ("podSelector", "namespaceSelector") and isinstance(child, dict):
                            for empty in ("matchLabels", "matchExpressions"):
                                if child.get(empty) in (None, {}, []):
                                    child.pop(empty, None)
                        selectors(child)
            selectors(normalized)
            return normalized

        if normalize(existing.get("spec")) == normalize(desired.get("spec")):
            return
        # Both UID and resourceVersion fence deletion/recreation and concurrent
        # edits after the GET. Never retry a conflict by adopting a new object.
        metadata.update(uid=observed["uid"], resourceVersion=observed["resourceVersion"])
        self.call("replace", "-f", "-", input=json.dumps(desired))

    def create_environment(self, resources: list[dict], ident: str) -> None:
        # A denied privileged Pod must not leave a PVC, policy or lease behind.
        self.check_admission(resources)
        existing_resources = []
        for resource in resources:
            existing = self.get(resource["kind"], resource["metadata"]["name"])
            if existing:
                self._validate_existing(resource, existing, ident)
            existing_resources.append(existing)
        self.check_storage(resources)
        for resource, existing in zip(resources, existing_resources):
            if existing:
                if resource["kind"] in ("StatefulSet", "PersistentVolumeClaim", "Service"):
                    continue
                resource["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
                self.call("replace", "-f", "-", input=json.dumps(resource))
            else:
                # Conflict if another actor won the name, never adopt it through apply.
                self.call("create", "-f", "-", input=json.dumps(resource))

    def wait(self, ident: str, timeout: int) -> None:
        """Wait for this owned Pod, failing promptly if it cannot become ready.

        A long kubectl watch can survive deletion while waiting for the original
        name to reappear. Polling also lets us report scheduling/init failures
        when the readiness deadline expires, without recreating the Pod.
        """
        if not re.fullmatch(r"[a-f0-9]{12}", ident):
            raise PodgroveError("Invalid environment identity; refusing readiness check")
        name = engine_pod_name(ident)
        deadline, uid, controller_uid = time.monotonic() + timeout, None, None
        while True:
            controller = self.get("statefulset", f"pg-{ident}")
            controller_meta = controller.get("metadata", {})
            controller_labels = controller_meta.get("labels", {})
            if not controller:
                raise PodgroveError(f"StatefulSet {self.namespace}/pg-{ident} was deleted or no longer exists while waiting for its Pod")
            if (controller_labels.get(MANAGED) != "podgrove" or controller_labels.get(ENVIRONMENT) != ident
                    or not controller_meta.get("uid")):
                raise PodgroveError(f"StatefulSet {self.namespace}/pg-{ident} is no longer owned by this worktree")
            if controller_meta.get("deletionTimestamp"):
                raise PodgroveError(f"StatefulSet {self.namespace}/pg-{ident} is being deleted")
            if controller_uid is not None and controller_meta["uid"] != controller_uid:
                raise PodgroveError(f"StatefulSet {self.namespace}/pg-{ident} was replaced while waiting for its Pod")
            controller_uid = controller_meta["uid"]
            pod = self.get("pod", name)
            if not pod:
                if uid is not None:
                    raise PodgroveError(f"Pod {self.namespace}/{name} was deleted or no longer exists while waiting for readiness")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    conditions = controller.get("status", {}).get("conditions", [])
                    details = "; ".join(str(item.get("message", item.get("reason", ""))) for item in conditions)[:1500]
                    raise PodgroveError(f"Timed out after {timeout}s waiting for StatefulSet {self.namespace}/pg-{ident} "
                                        f"to create Pod {name}: {details or 'controller has not created the Pod'}")
                time.sleep(min(2, remaining))
                continue
            metadata, status = pod.get("metadata", {}), pod.get("status", {})
            self._validate_pod_controller(pod, controller, ident)
            if uid is not None and metadata.get("uid") != uid:
                raise PodgroveError(f"Pod {self.namespace}/{name} was replaced while waiting for readiness")
            uid = metadata.get("uid")
            if metadata.get("deletionTimestamp"):
                raise PodgroveError(f"Pod {self.namespace}/{name} is being deleted since {metadata['deletionTimestamp']}")
            phase = status.get("phase", "Unknown")
            details = [f"phase={phase}"]
            for field in ("reason", "message"):
                if status.get(field):
                    details.append(str(status[field]))
            for condition in status.get("conditions", []):
                if condition.get("status") != "True" and condition.get("message"):
                    details.append(f"{condition.get('type', 'condition')}: {condition['message']}")
            for container in status.get("initContainerStatuses", []) + status.get("containerStatuses", []):
                current = container.get("state", {})
                problem = current.get("waiting") or current.get("terminated")
                if problem and problem.get("reason") != "Completed":
                    details.append(f"{container.get('name', 'container')}: {problem.get('reason', '')} {problem.get('message', '')}".strip())
            summary = "; ".join(details)[:1500]
            if phase in ("Failed", "Succeeded"):
                raise PodgroveError(f"Pod {self.namespace}/{name} reached terminal phase {phase}: {summary}")
            if any(condition.get("type") == "Ready" and condition.get("status") == "True"
                   for condition in status.get("conditions", [])):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PodgroveError(f"Timed out after {timeout}s waiting for Pod {self.namespace}/{name} readiness: {summary}")
            time.sleep(min(2, remaining))

    def heartbeat(self, ident: str, timestamp: float) -> None:
        lease = self.get("configmap", f"pg-{ident}")
        labels = lease.get("metadata", {}).get("labels", {})
        if labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != ident:
            raise PodgroveError("Environment lease is missing or no longer owned by this worktree")
        if self.lease_mode(ident, lease) != self.namespace_mode:
            raise PodgroveError("Environment lease namespace_mode changed; refusing activity update")
        lease["data"]["last_activity"] = str(timestamp)
        self.call("replace", "-f", "-", input=json.dumps(lease))

    def destroy(self, ident: str, *, namespace_mode: str | None = None) -> None:
        if not re.fullmatch(r"[a-f0-9]{12}", ident):
            raise PodgroveError("Invalid environment identity; refusing deletion")
        mode = resolve_namespace_mode(self.namespace, namespace_mode if namespace_mode is not None else self.namespace_mode)
        lease = self.get("configmap", f"pg-{ident}")
        if lease and self.lease_mode(ident, lease) != mode:
            raise PodgroveError("Environment lease namespace_mode differs from cleanup authority; refusing deletion")
        selector = f"{MANAGED}=podgrove,{ENVIRONMENT}={ident}"
        # Stop the real controller and wait for its Pod before removing storage.
        # No force deletion: Kubernetes' StatefulSet identity guarantee requires
        # observing termination before a replacement can use the same PVC.
        self.call("delete", "statefulset", "-l", selector, "--ignore-not-found", "--cascade=foreground",
                  "--wait=true", "--timeout=120s", "--request-timeout=0", timeout=130)
        # Retain namespace and administrator bootstrap objects in EVERY mode,
        # including legacy exclusive records. Neither needs cluster-scoped access.
        # Fixed resource types and a conjunctive owner selector; never delete all.
        self.call("delete", "pod,pvc,configmap,networkpolicy,service", "-l", selector, "--ignore-not-found", "--wait=true", "--timeout=120s", timeout=130)
