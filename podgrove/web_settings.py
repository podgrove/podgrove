"""Bounded, named GET observations for the dashboard's access configuration page.

This reports declarations, not effective authorization. It neither discovers
arbitrary RBAC nor reads kubeconfig, credentials, Secrets or ServiceAccount tokens.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import re
import time
from typing import Callable

from .bootstrap import PROVISIONING_MARKER
from .errors import PodgroveError
from .kube import Kube, context_name, namespace_name

READ_BUDGET = 15.0
READ_TIMEOUT = 6.0
READ_LIMIT = 256 * 1024
OBJECT_LIMIT = 16 * 1024
MAX_NAMESPACES = 256
MAX_WORKERS = 4


@dataclass(frozen=True)
class Resource:
    kind: str
    resource: str
    name: str
    namespace: str | None = None
    optional: bool = False


def _resources(namespace: str) -> list[Resource]:
    namespace_name(namespace)
    resources = [Resource("ConfigMap", "configmaps", PROVISIONING_MARKER, namespace)]
    for kind, resource in (("ServiceAccount", "serviceaccounts"),
                           ("Role", "roles.rbac.authorization.k8s.io"),
                           ("RoleBinding", "rolebindings.rbac.authorization.k8s.io")):
        for name in ("podgrove-client", "podgrove-reaper"):
            resources.append(Resource(kind, resource, name, namespace))
    resources.append(Resource("RoleBinding", "rolebindings.rbac.authorization.k8s.io",
                              "podgrove-developers", namespace))
    return resources


def _base(resource: Resource, status: str, warning: str | None = None) -> dict:
    return {"kind": resource.kind, "name": resource.name, "namespace": resource.namespace,
            "optional": resource.optional, "status": status, "warning": warning}


class _SafeFields:
    def __init__(self):
        self.truncated = False

    def text(self, value, limit: int = 128) -> str:
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise ValueError("Invalid metadata field")
        self.truncated |= len(value) > limit
        return value[:limit]

    def values(self, value) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("Invalid metadata list")
        self.truncated |= len(value) > 16
        return [self.text(item) for item in value[:16]]


def _sanitize(resource: Resource, body: dict) -> dict:
    if resource.namespace is None or resource.kind not in {"ConfigMap", "ServiceAccount", "Role", "RoleBinding"}:
        raise ValueError("Only namespaced Podgrove metadata can be inspected")
    if not isinstance(body, dict):
        raise ValueError("Invalid object")
    metadata = body.get("metadata")
    if (body.get("kind") != resource.kind or not isinstance(metadata, dict)
            or metadata.get("name") != resource.name or not metadata.get("uid")
            or metadata.get("namespace") != resource.namespace):
        raise ValueError("Unexpected object identity")
    safe = _SafeFields()
    result = _base(resource, "present")
    result.update(uid=safe.text(metadata["uid"]), created_at=safe.text(metadata.get("creationTimestamp", "")),
                  terminating=bool(metadata.get("deletionTimestamp")))
    if resource.kind == "ConfigMap":
        labels = metadata.get("labels", {})
        data = body.get("data", {})
        if (resource.name != PROVISIONING_MARKER or not isinstance(labels, dict)
                or labels.get("app.kubernetes.io/managed-by") != "podgrove"
                or labels.get("podgrove.dev/component") != "bootstrap"
                or "podgrove.dev/environment" in labels or not isinstance(data, dict)
                or data.get("version") != "1" or data.get("namespace_mode") not in {"shared", "worktree"}):
            raise ValueError("Invalid provisioning marker")
        environment = data.get("environment")
        if ((data["namespace_mode"] == "shared" and "environment" in data)
                or (data["namespace_mode"] == "worktree"
                    and (not isinstance(environment, str) or not re.fullmatch(r"[a-f0-9]{12}", environment)))):
            raise ValueError("Invalid provisioning marker identity")
        result.update(version="1", namespace_mode=data["namespace_mode"], environment=environment)
    elif resource.kind == "ServiceAccount":
        automount = body.get("automountServiceAccountToken")
        if automount is not None and not isinstance(automount, bool):
            raise ValueError("Invalid automount setting")
        result["automount_service_account_token"] = automount
    elif resource.kind == "Role":
        rules = body.get("rules", [])
        if not isinstance(rules, list):
            raise ValueError("Invalid rules")
        safe.truncated |= len(rules) > 32
        result["rules"] = []
        for rule in rules[:32]:
            if not isinstance(rule, dict):
                raise ValueError("Invalid rule")
            result["rules"].append({target: safe.values(rule.get(source, [])) for source, target in (
                ("apiGroups", "api_groups"), ("resources", "resources"), ("resourceNames", "resource_names"),
                ("verbs", "verbs"), ("nonResourceURLs", "non_resource_urls"))})
    elif resource.kind == "RoleBinding":
        reference, subjects = body.get("roleRef"), body.get("subjects", [])
        if not isinstance(reference, dict) or not isinstance(subjects, list):
            raise ValueError("Invalid role binding")
        if reference.get("kind") not in ("Role", "ClusterRole"):
            raise ValueError("Invalid role reference")
        result["role_ref"] = {"api_group": safe.text(reference.get("apiGroup", "")),
                              "kind": reference["kind"], "name": safe.text(reference["name"])}
        safe.truncated |= len(subjects) > 64
        result["subjects"] = []
        for subject in subjects[:64]:
            if not isinstance(subject, dict) or subject.get("kind") not in ("ServiceAccount", "User", "Group"):
                raise ValueError("Invalid subject")
            result["subjects"].append({"kind": subject["kind"], "name": safe.text(subject["name"]),
                                       "namespace": safe.text(subject["namespace"]) if "namespace" in subject else None})
    # Keep the combined page comfortably below the dashboard's response cap.
    for key in ("rules", "subjects"):
        while result.get(key) and len(json.dumps(result).encode()) > OBJECT_LIMIT:
            result[key].pop()
            safe.truncated = True
    result["truncated"] = safe.truncated
    return result


def settings(context: str, namespaces: list[str], *, namespace: str | None = None,
             read_command: Callable | None = None) -> dict:
    """Inspect one selected, previously authorized/local namespace with named GETs.

    The caller supplies only its explicit namespace filter or namespaces from
    validated local records. No namespace options means zero cluster reads.
    """
    from .web import WebError, bounded_read_command

    try:
        context = context_name(context)
    except PodgroveError as exc:
        raise WebError("An explicit valid cluster context is required", 400) from exc
    if not isinstance(namespaces, list) or len(namespaces) > MAX_NAMESPACES:
        raise WebError("Too many namespace options; start web with an explicit --namespace", 400)
    try:
        options = sorted({namespace_name(item) for item in namespaces if isinstance(item, str)})
        if len(options) != len(set(namespaces)):
            raise ValueError("Invalid namespace option")
    except (PodgroveError, TypeError, ValueError) as exc:
        raise WebError("Namespace scope is invalid", 400) from exc
    if namespace is not None and (not isinstance(namespace, str) or namespace not in options):
        raise WebError("Namespace is outside this dashboard's local or explicit scope", 400)
    selected = namespace if namespace is not None else (options[0] if options else None)
    response = {
        "context": context, "read_only": True, "observed_at": time.time(), "namespace_options": options,
        "selected_namespace": selected, "namespace": None, "provisioning": None,
        "access": {"objects": [], "note": "Named Podgrove declarations in this namespace only. These roles do not establish your effective permissions; other grants and admission policies may apply."},
        "bootstrap": {"namespace": selected, "status": "reference_only",
                      "note": "These names match the namespaced bootstrap. This page never reads or modifies Namespace objects or cluster-wide access. The provisioning ConfigMap records the intended mode; missing objects do not prove authorization."},
        "warnings": [],
    }
    if selected is None:
        response["warnings"].append("No namespace is in local scope. Start web with --namespace, or create a local environment record, to inspect configuration.")
        return response
    reader = read_command or bounded_read_command
    kube = Kube(context, selected)
    deadline = time.monotonic() + READ_BUDGET

    def observe(resource: Resource) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _base(resource, "not_checked", "The page read budget was reached; refresh to retry.")
        try:
            command = kube.command("get", resource.resource, resource.name, "-o", "json", "--ignore-not-found")
            # A subprocess deadline also bounds authentication-plugin stalls.
            raw = reader(command, timeout=min(READ_TIMEOUT, remaining), limit=READ_LIMIT)
            if not isinstance(raw, bytes) or len(raw) > READ_LIMIT:
                raise ValueError("Invalid response size")
            if not raw.strip():
                return _base(resource, "missing")
            return _sanitize(resource, json.loads(raw))
        except (WebError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            # Do not expose raw Kubernetes errors, plugin arguments or payloads.
            return _base(resource, "inaccessible", "Metadata could not be read or verified; check connectivity and GET permissions.")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="podgrove-settings") as pool:
        observed = list(pool.map(observe, _resources(selected)))
    response["provisioning"], response["access"]["objects"] = observed[0], observed[1:]
    if any(item["status"] in ("inaccessible", "not_checked") for item in observed):
        response["warnings"].append("Some named resources could not be checked. Unreadable metadata is not evidence that a resource is absent.")
    if any(item.get("truncated") for item in observed):
        response["warnings"].append("Some access metadata was truncated to keep this page bounded.")
    response["observed_at"] = time.time()
    return response
