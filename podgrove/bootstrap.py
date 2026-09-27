"""Offline installation of Podgrove-owned objects in one existing namespace.

The renderer never emits cluster-scoped objects, namespace-wide quotas or limits,
Namespace mutations, or admission policies. It cannot inspect existing workloads.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

import yaml

from .errors import PodgroveError

MANAGED = "app.kubernetes.io/managed-by"
ENVIRONMENT = "podgrove.dev/environment"
RBAC = "rbac.authorization.k8s.io"
DEFAULT_DEVELOPER_GROUP = "podgrove-developers"
PROVISIONING_MARKER = "podgrove-bootstrap"
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\Z")


def _namespace(value: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or not _LABEL.fullmatch(value):
        raise PodgroveError("Bootstrap namespace must be a DNS label of at most 63 characters")
    return value


def bootstrap_names(namespace: str) -> dict[str, str]:
    """Names are local to the selected namespace, never cluster-scoped."""
    _namespace(namespace)
    return {"marker": PROVISIONING_MARKER, "client": "podgrove-client", "reaper": "podgrove-reaper",
            "developers": "podgrove-developers", "network_policy": "podgrove-default-deny"}


def _storage_class(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 253 or not value
            or any(len(part) > 63 or not _LABEL.fullmatch(part) for part in value.split("."))):
        raise PodgroveError("Invalid storage class (DNS subdomain)")
    return value


def _developer_group(value: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise PodgroveError("Developer group must be a nonempty name without control characters")
    return value


def _object(kind, name, namespace, **body):
    groups = {"ServiceAccount": "v1", "ConfigMap": "v1", "Role": f"{RBAC}/v1",
              "RoleBinding": f"{RBAC}/v1", "NetworkPolicy": "networking.k8s.io/v1"}
    if kind not in groups:
        raise PodgroveError("Bootstrap supports only Podgrove-owned namespaced resources")
    return {"apiVersion": groups[kind], "kind": kind,
            "metadata": {"name": name, "namespace": namespace}, **body}


def _rule(resources, verbs, *, group="", names=None):
    rule = {"apiGroups": [group], "resources": resources, "verbs": verbs}
    if names is not None:
        rule["resourceNames"] = names
    return rule


def _binding(name, role, subjects, namespace):
    return _object("RoleBinding", name, namespace,
                   roleRef={"apiGroup": RBAC, "kind": "Role", "name": role}, subjects=subjects)


def provisioning_marker(namespace: str, namespace_mode: str, identity: str | None = None) -> dict:
    """Runtime checks this exact marker instead of reading or labelling a Namespace."""
    _namespace(namespace)
    if namespace_mode not in {"shared", "worktree"}:
        raise PodgroveError("Bootstrap namespace mode must be shared or worktree")
    data = {"version": "1", "namespace_mode": namespace_mode}
    if namespace_mode == "worktree":
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{12}", identity):
            raise PodgroveError("Worktree bootstrap requires its exact 12-character environment identity")
        data["environment"] = identity
    elif identity is not None:
        raise PodgroveError("Shared bootstrap must not bind the namespace to one environment identity")
    marker = _object("ConfigMap", PROVISIONING_MARKER, namespace, data=data)
    marker["metadata"]["labels"] = {MANAGED: "podgrove", "podgrove.dev/component": "bootstrap"}
    return marker


def render_bootstrap(namespace: str, storage_class: str | None = None,
                     developer_group: str = DEFAULT_DEVELOPER_GROUP, include_tainted_nodes: bool = False,
                     *, namespace_mode: str = "shared", identity: str | None = None) -> dict[str, list[dict]]:
    """Render only Podgrove-owned objects for an operator-created namespace.

    storage_class is accepted for compatibility but grants no storage reads or
    changes. Node-read installation is unsupported: every grant stays namespaced.
    """
    _namespace(namespace)
    if namespace.startswith("kube-"):
        raise PodgroveError("Bootstrap must not target reserved kube-* namespaces")
    if storage_class is not None:
        _storage_class(storage_class)
    _developer_group(developer_group)
    if include_tainted_nodes is not False:
        raise PodgroveError("Node-reader bootstrap is unsupported; Podgrove grants only namespaced access")
    marker = provisioning_marker(namespace, namespace_mode, identity)
    client = {"kind": "ServiceAccount", "name": "podgrove-client", "namespace": namespace}
    reaper = {"kind": "ServiceAccount", "name": "podgrove-reaper", "namespace": namespace}
    human = {"kind": "Group", "name": developer_group, "apiGroup": RBAC}
    workload_verbs = ["get", "list", "watch", "create", "patch", "update", "delete"]
    client_rules = [
        _rule(["pods", "persistentvolumeclaims", "configmaps", "services"], workload_verbs),
        _rule(["pods/portforward", "pods/exec"], ["get", "create"]),
        _rule(["pods/log"], ["get"]),
        _rule(["serviceaccounts"], ["get"], names=["podgrove-client", "podgrove-reaper"]),
        _rule(["roles"], ["get"], group=RBAC, names=["podgrove-client", "podgrove-reaper"]),
        _rule(["rolebindings"], ["get"], group=RBAC,
              names=["podgrove-client", "podgrove-reaper", "podgrove-developers"]),
        _rule(["networkpolicies"], workload_verbs, group="networking.k8s.io"),
        _rule(["statefulsets"], workload_verbs, group="apps"),
    ]
    cleanup_verbs = ["get", "list", "watch", "delete"]
    policy = _object("NetworkPolicy", "podgrove-default-deny", namespace, spec={
        "podSelector": {"matchLabels": {MANAGED: "podgrove"}},
        "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": [],
    })
    policy["metadata"]["labels"] = {MANAGED: "podgrove"}
    return {
        "00-provisioning-marker.yaml": [marker],
        "05-network-isolation.yaml": [policy],
        "10-client-rbac.yaml": [
            _object("ServiceAccount", "podgrove-client", namespace, automountServiceAccountToken=False),
            _object("Role", "podgrove-client", namespace, rules=client_rules),
            _binding("podgrove-client", "podgrove-client", [client], namespace),
        ],
        "30-developer-bindings.yaml": [_binding("podgrove-developers", "podgrove-client", [human], namespace)],
        "40-reaper-rbac.yaml": [
            _object("ServiceAccount", "podgrove-reaper", namespace, automountServiceAccountToken=False),
            _object("Role", "podgrove-reaper", namespace, rules=[
                _rule(["pods", "persistentvolumeclaims", "configmaps", "services"], cleanup_verbs),
                _rule(["statefulsets"], cleanup_verbs, group="apps"),
                _rule(["networkpolicies"], cleanup_verbs, group="networking.k8s.io"),
            ]),
            _binding("podgrove-reaper", "podgrove-reaper", [reaper], namespace),
        ],
    }


def _open_directory(path: Path) -> int:
    """Walk ordinary directories without following intermediate symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _same_directory(parent_fd: int, name: str, expected) -> bool:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    return (stat.S_ISDIR(current.st_mode)
            and (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino))


def _unchanged_file(directory_fd: int, name: str, identity: tuple[int, int], payload: bytes) -> bool:
    """Check an ordinary file before reading it; preserve changed user content."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
            return False
        if stream.read(len(payload) + 1) != payload:
            return False
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    return stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity


def generate_bootstrap(output: Path, *, namespace: str, storage_class: str | None = None,
                       developer_group: str = DEFAULT_DEVELOPER_GROUP,
                       include_tainted_nodes: bool = False, namespace_mode: str = "shared",
                       identity: str | None = None) -> list[Path]:
    """Create a new manifest-only directory, refusing existing paths/symlinks."""
    documents = render_bootstrap(namespace, storage_class, developer_group, include_tainted_nodes,
                                 namespace_mode=namespace_mode, identity=identity)
    payloads = {name: yaml.safe_dump_all(items, sort_keys=False) for name, items in documents.items()}
    path = Path(os.path.abspath(output))
    parent_fd = directory_fd = None
    created = {}
    directory_identity = None
    try:
        parent_fd = _open_directory(path.parent)
        os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        directory_identity = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        directory_fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        opened = os.fstat(directory_fd)
        if (not stat.S_ISDIR(directory_identity.st_mode)
                or (opened.st_dev, opened.st_ino) != (directory_identity.st_dev, directory_identity.st_ino)):
            raise PodgroveError("Bootstrap output directory changed before writing; refusing to continue")
        for name, payload in payloads.items():
            if not _same_directory(parent_fd, path.name, directory_identity):
                raise PodgroveError("Bootstrap output directory changed while writing; refusing to continue")
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory_fd)
            with os.fdopen(fd, "w") as stream:
                info = os.fstat(stream.fileno())
                stream.write(payload)
            # Record completed writes only. A failed/partial write is left for
            # review rather than risking deletion of concurrently edited data.
            created[name] = ((info.st_dev, info.st_ino), payload.encode())
        if not _same_directory(parent_fd, path.name, directory_identity):
            raise PodgroveError("Bootstrap output directory changed while writing; refusing to continue")
        if set(os.listdir(directory_fd)) != set(created) or any(
                not _unchanged_file(directory_fd, name, identity, payload)
                for name, (identity, payload) in created.items()):
            raise PodgroveError("Bootstrap output files changed while writing; refusing to continue")
        return [path / name for name in payloads]
    except (OSError, PodgroveError) as error:
        if directory_fd is not None and directory_identity is not None:
            try:
                if _same_directory(parent_fd, path.name, directory_identity):
                    for name, (identity, payload) in created.items():
                        if _unchanged_file(directory_fd, name, identity, payload):
                            os.unlink(name, dir_fd=directory_fd)
                    os.rmdir(path.name, dir_fd=parent_fd)
            except OSError:
                pass  # Preserve a changed path or unrelated file; never recursively remove it.
        partial = (f" Partial files may remain at {path}; inspect them before removal."
                   if directory_identity is not None else "")
        raise PodgroveError("Cannot generate bootstrap: use a new output directory with an existing, "
                            f"ordinary parent directory (no symlinks): {error}.{partial}") from error
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        if parent_fd is not None:
            os.close(parent_fd)
