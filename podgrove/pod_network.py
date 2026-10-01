"""Mutually declared, namespace-scoped networking between worktree engines."""
from __future__ import annotations

import copy
import fnmatch
import json
import re
import threading
import time
from urllib.parse import urlencode

from .errors import PodgroveError
from .kube import Kube
from .network import network_settings, open_publications, policy_spec, validate_model
from .repository import WORKTREE_NAME

MANAGED = "app.kubernetes.io/managed-by"
ENVIRONMENT = "podgrove.dev/environment"
DECLARATION = "pod_network"
MAX_PEERS = 128
INTERVAL = 5
RECONCILE_TIMEOUT = 30
CLEANUP_TIMEOUT = 10
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DECLARATION_BYTES = 32768
SIZING_UID = "0" * 36


def declaration(settings, name, ports, pod_uid=None):
    return {"version": 1, "worktree": name, "network": network_settings(settings),
            "ports": ports, "pod_uid": pod_uid}


def declaration_text(profile):
    return json.dumps(profile, sort_keys=True, separators=(",", ":"))


def offline_ports(settings, model):
    """Ports a declaration can advertise, known before any engine exists."""
    if settings.get("pod_to_pod", "disabled") == "selected":
        return validate_model(settings, model)
    validate_model(settings, model)
    return [{"service": name, "target": target if type(target) is int else 65535, "published": published or 65535}
            for name, target, published in open_publications(model)]


def check_declaration(network, model, name):
    """Refuse offline a configuration whose peer declaration would exceed the lease limit."""
    settings = network_settings(network)
    ports = offline_ports(settings, model)
    if settings.get("pod_to_pod", "disabled") == "disabled":
        return
    size = len(declaration_text(declaration(settings, name, ports, SIZING_UID)).encode("utf-8"))
    if size > MAX_DECLARATION_BYTES:
        raise PodgroveError(f"network: this worktree's peer declaration renders to {size} bytes; the limit is "
                            f"{MAX_DECLARATION_BYTES} bytes (32 KiB). Reduce network.expose, network.connect or published ports")


def addresses(namespace, ident, ports):
    host = f"pg-{ident}-0.pg-{ident}.{namespace}.svc.cluster.local"
    return [{"service": item["service"], "target": item["target"], "port": item["published"],
             "host": host, "url": f"http://{host}:{item['published']}"} for item in ports]


def observed_ports(settings, model, rows):
    declared = validate_model(settings, model)
    observed = set()
    counts = {}
    if not isinstance(rows, list):
        raise PodgroveError("Pod networking requires a bounded service observation list")
    for row in rows:
        if not isinstance(row, dict):
            raise PodgroveError("Pod networking service observation is malformed")
        if row.get("State") != "running" or row.get("Service") not in model["services"]:
            continue
        if model.get("name") and row.get("Project") != model["name"]:
            continue
        counts[row["Service"]] = counts.get(row["Service"], 0) + 1
        for port in row.get("Publishers") or []:
            if not isinstance(port, dict):
                raise PodgroveError("Pod networking published port observation is malformed")
            if port.get("Protocol") == "tcp" and port.get("URL") == "0.0.0.0":
                target, published = port.get("TargetPort"), port.get("PublishedPort")
                if (type(target) is int and type(published) is int and 1 <= target <= 65535 and 1 <= published <= 65535
                        and published not in (2375, 2376)):
                    observed.add((row["Service"], target, published))
    if settings.get("pod_to_pod", "disabled") == "selected":
        for item in declared:
            if counts.get(item["service"]) != 1 or (item["service"], item["target"], item["published"]) not in observed:
                raise PodgroveError(f"network.expose: {item['service']}:{item['published']} is not running with a "
                                    "verified 0.0.0.0 published binding; peer access remains closed")
        return declared
    if len(observed) > 128:
        raise PodgroveError("network.pod_to_pod open: more than 128 published TCP ports cannot be advertised")
    return [{"service": service, "target": target, "published": published}
            for service, target, published in sorted(observed)]


def _matches(rule, namespace, name):
    return rule["namespace"] == namespace and fnmatch.fnmatchcase(name, rule.get("worktree", "*"))


def _peer(namespace, ident, name):
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
            "podSelector": {"matchLabels": {MANAGED: "podgrove", ENVIRONMENT: ident, WORKTREE_NAME: name,
                                             "statefulset.kubernetes.io/pod-name": f"pg-{ident}-0"}}}


def selected_rules(namespace, ident, own, peers):
    """Resolve both declarations before rendering one exact peer and port allowance."""
    ingress, egress, pending = [], [], []
    settings, name = own["network"], own["worktree"]
    if not isinstance(peers, list) or len(peers) > MAX_PEERS:
        raise PodgroveError(f"Pod networking discovery exceeds {MAX_PEERS} total peers")
    if settings.get("pod_to_pod", "disabled") != "selected":
        return ingress, egress, pending
    targets = set()
    for peer in peers:
        if (peer["namespace"], peer["identity"]) == (namespace, ident):
            continue
        remote = peer["declaration"]
        remote_settings = remote["network"]
        if remote_settings.get("pod_to_pod", "disabled") != "selected":
            continue
        peer_name, peer_namespace = remote["worktree"], peer["namespace"]
        to = _peer(peer_namespace, peer["identity"], peer_name)
        for connection in settings.get("connect", []):
            if not _matches(connection, peer_namespace, peer_name):
                continue
            targets.add((connection["namespace"], connection["worktree"]))
            permitted = {port["published"] for exposure in remote_settings["expose"]
                         if any(_matches(source, namespace, name) for source in exposure["from"])
                         for port in remote["ports"] if port["service"] == exposure["service"]}
            missing = sorted(set(connection["ports"]) - permitted)
            if missing:
                pending.append(f"{peer_namespace}/{peer_name}: ports {missing} have no reciprocal published network.expose")
                continue
            egress.append({"to": [to], "ports": [{"protocol": "TCP", "port": port} for port in sorted(connection["ports"])]})
        accepted = {port["published"] for exposure in settings["expose"]
                    if any(_matches(source, peer_namespace, peer_name) for source in exposure["from"])
                    for port in own["ports"] if port["service"] == exposure["service"]}
        for connection in remote_settings["connect"]:
            if _matches(connection, namespace, name) and set(connection["ports"]) <= accepted:
                ingress.append({"from": [to], "ports": [{"protocol": "TCP", "port": port} for port in sorted(connection["ports"])]})
    for connection in settings.get("connect", []):
        if (connection["namespace"], connection["worktree"]) not in targets:
            pending.append(f"{connection['namespace']}/{connection['worktree']}: no ready selected peer")
    def unique(rules):
        return [json.loads(value) for value in sorted({json.dumps(rule, sort_keys=True) for rule in rules})]
    return unique(ingress), unique(egress), sorted(set(pending))


def _metadata(resource):
    if not isinstance(resource, dict) or not isinstance(resource.get("metadata"), dict):
        raise PodgroveError("Pod networking object metadata is malformed")
    return resource["metadata"]


def _identifier(value):
    return isinstance(value, str) and bool(value) and len(value) <= 128 and not re.search(r"[\x00-\x20\x7f]", value)


def _owned(resource, namespace, ident, name):
    meta = _metadata(resource)
    labels = meta.get("labels", {})
    if (not isinstance(labels, dict) or meta.get("namespace") != namespace or meta.get("name") != name
            or not _identifier(meta.get("uid")) or not _identifier(meta.get("resourceVersion")) or meta.get("deletionTimestamp")
            or labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != ident):
        raise PodgroveError("Pod networking ownership is missing, replaced, or being deleted")
    return meta


def _owners(meta):
    owners = meta.get("ownerReferences", [])
    if not isinstance(owners, list) or any(not isinstance(owner, dict) for owner in owners):
        raise PodgroveError("Pod networking controller references are malformed")
    return owners


def _checked_declaration(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_DECLARATION_BYTES:
        raise PodgroveError("Peer network declaration is missing or exceeds 32 KiB")
    try:
        profile = json.loads(value)
        if (not isinstance(profile, dict) or set(profile) != {"version", "worktree", "network", "ports", "pod_uid"}
                or type(profile["version"]) is not int or profile["version"] != 1):
            raise ValueError
        if not isinstance(profile["worktree"], str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?", profile["worktree"]):
            raise ValueError
        if not isinstance(profile["network"], dict):
            raise ValueError
        profile["network"] = network_settings(profile["network"])
        if profile["pod_uid"] is not None and not _identifier(profile["pod_uid"]):
            raise ValueError
        if not isinstance(profile["ports"], list) or len(profile["ports"]) > 128:
            raise ValueError
        for item in profile["ports"]:
            if (not isinstance(item, dict) or set(item) != {"service", "target", "published"}
                    or not isinstance(item["service"], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", item["service"])
                    or item["published"] in (2375, 2376)
                    or any(type(item[key]) is not int or not 1 <= item[key] <= 65535 for key in ("target", "published"))):
                raise ValueError
        return profile
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        raise PodgroveError("Peer network declaration is malformed; refusing its grants") from error


def _reason(error):
    return str(error) if isinstance(error, PodgroveError) else f"{type(error).__name__}: {error}"


class _BoundedKube(Kube):
    def __init__(self, manager):
        super().__init__(manager.kube.context, manager.kube.namespace,
                         namespace_mode=getattr(manager.kube, "namespace_mode", "shared"))
        self.manager = manager

    def call(self, *args, **kwargs):
        return self.manager._call(*args, **kwargs)


class _EitherCancelled:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event is not None and event.is_set() for event in self.events)


class PodNetwork:
    def __init__(self, kube, ident, name, settings, model, *, expected_uids, on_change=None):
        self.kube, self.ident, self.name = kube, ident, name
        self.settings, self.model = network_settings(settings), model
        self.expected_uids = expected_uids
        self.on_change = on_change
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.reconciling = threading.Lock()
        self.deadline = None
        self.cancel_event = self.stopping
        self.worker = None
        self.rows = []
        self.lease_uid = None
        self.current = {"state": "starting", "mode": self.settings.get("pod_to_pod", "disabled"), "endpoints": []}

    def _call(self, *args, **kwargs):
        remaining = self.deadline - time.monotonic() if self.deadline is not None else 15
        if self.cancel_event.is_set() or remaining <= 0:
            raise PodgroveError("Pod networking reconciliation cancelled or its deadline expired")
        timeout = min(15, remaining, kwargs.pop("timeout", 15))
        result = self.kube.call(*args, timeout=timeout, cancel_event=self.cancel_event, **kwargs)
        if len(result.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise PodgroveError("Pod networking API response exceeds 8 MiB")
        if self.cancel_event.is_set() or self.deadline is not None and time.monotonic() >= self.deadline:
            raise PodgroveError("Pod networking reconciliation cancelled or its deadline expired")
        return result

    def _get(self, kind, name):
        result = self._call("get", kind, name, "-o", "json", "--ignore-not-found")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def _list(self, namespace, kind):
        """One server-bounded labelled list per kind per namespace."""
        query = urlencode({"labelSelector": f"{MANAGED}=podgrove", "limit": MAX_PEERS})
        response = self._call("get", "--raw", f"/api/v1/namespaces/{namespace}/{kind}?{query}")
        value = json.loads(response.stdout)
        if not isinstance(value, dict) or not isinstance(value.get("metadata", {}), dict):
            raise PodgroveError("Pod networking discovery response is malformed")
        items = value.get("items")
        if not isinstance(items, list) or len(items) > MAX_PEERS or value.get("metadata", {}).get("continue"):
            raise PodgroveError(f"Pod networking discovery in {namespace} exceeds {MAX_PEERS} objects")
        return items

    def _own_controller(self):
        controller = self._get("statefulset", f"pg-{self.ident}")
        meta = _owned(controller, self.kube.namespace, self.ident, f"pg-{self.ident}")
        if meta["uid"] != self.expected_uids["statefulset_uid"]:
            raise PodgroveError("Pod networking engine controller changed")

    def _own_lease(self):
        lease = self._get("configmap", f"pg-{self.ident}")
        meta = _owned(lease, self.kube.namespace, self.ident, f"pg-{self.ident}")
        if self.lease_uid is not None and meta["uid"] != self.lease_uid:
            raise PodgroveError("Pod networking lease identity changed")
        self.lease_uid = meta["uid"]
        if not isinstance(lease.get("data"), dict):
            raise PodgroveError("Pod networking lease data is malformed")
        return lease

    def _own_pod(self):
        pod = self._get("pod", f"pg-{self.ident}-0")
        meta = _owned(pod, self.kube.namespace, self.ident, f"pg-{self.ident}-0")
        owners = _owners(meta)
        if (meta["uid"] != self.expected_uids["pod_uid"] or meta["labels"].get(WORKTREE_NAME) != self.name
                or not any(owner.get("kind") == "StatefulSet" and owner.get("controller") is True
                           and owner.get("name") == f"pg-{self.ident}"
                           and owner.get("uid") == self.expected_uids["statefulset_uid"] for owner in owners)):
            raise PodgroveError("Pod networking engine identity changed")

    def _publish(self, profile, lease):
        text = declaration_text(profile)
        _checked_declaration(text)
        if lease.get("data", {}).get(DECLARATION) == text:
            return
        meta = lease["metadata"]
        patch = [{"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
                 {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
                 {"op": "add", "path": f"/data/{DECLARATION}", "value": text}]
        self._call("patch", "configmap", meta["name"], "--type=json", "-p", json.dumps(patch))

    def _peer(self, namespace, lease, pods):
        """Resolve one advertised peer; any defect skips only that peer."""
        metadata = _metadata(lease)
        data = lease.get("data", {})
        if not isinstance(data, dict) or not isinstance(metadata.get("labels", {}), dict):
            raise PodgroveError("lease data or labels are malformed")
        value = data.get(DECLARATION)
        if value is None:
            return None
        ident = metadata.get("labels", {}).get(ENVIRONMENT)
        if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{12}", ident):
            raise PodgroveError("invalid environment identity")
        _owned(lease, namespace, ident, f"pg-{ident}")
        profile = _checked_declaration(value)
        pod = pods.get(f"pg-{ident}-0")
        if not pod or not profile["pod_uid"]:
            return None
        meta = _owned(pod, namespace, ident, f"pg-{ident}-0")
        owners = _owners(meta)
        status = pod.get("status", {})
        if not isinstance(status, dict) or not isinstance(status.get("conditions", []), list):
            raise PodgroveError("Pod status is malformed")
        conditions = status.get("conditions", [])
        if any(not isinstance(condition, dict) for condition in conditions):
            raise PodgroveError("Pod readiness is malformed")
        if (meta["uid"] != profile["pod_uid"] or meta["labels"].get(WORKTREE_NAME) != profile["worktree"]
                or not any(owner.get("kind") == "StatefulSet" and owner.get("name") == f"pg-{ident}"
                           and owner.get("controller") is True and _identifier(owner.get("uid")) for owner in owners)
                or not any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions)):
            return None
        return {"namespace": namespace, "identity": ident, "declaration": profile}

    def _peers(self):
        namespaces = {rule["namespace"] for rule in self.settings["connect"]}
        namespaces.update(source["namespace"] for rule in self.settings["expose"] for source in rule["from"])
        peers, skipped = [], []
        for namespace in sorted(namespaces):
            pods = {}
            for pod in self._list(namespace, "pods"):
                name = pod.get("metadata", {}).get("name") if isinstance(pod, dict) and isinstance(pod.get("metadata"), dict) else None
                if not isinstance(name, str):
                    skipped.append(f"{namespace}: ignored a managed Pod with malformed metadata")
                    continue
                pods[name] = None if name in pods else pod
            for lease in self._list(namespace, "configmaps"):
                label = lease.get("metadata", {}).get("name") if isinstance(lease, dict) and isinstance(lease.get("metadata"), dict) else None
                try:
                    found = self._peer(namespace, lease, pods)
                except Exception as error:  # any defect in one peer's data skips only that peer
                    skipped.append(f"{namespace}/{label if isinstance(label, str) else '?'}: peer ignored: {error}")
                    continue
                if found:
                    peers.append(found)
                    if len(peers) > MAX_PEERS:
                        raise PodgroveError(f"Pod networking discovery exceeds {MAX_PEERS} total peers")
        return peers, skipped

    def _apply(self, ingress=None, egress=None):
        resource = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                    "metadata": {"name": f"pg-{self.ident}", "namespace": self.kube.namespace,
                                 "labels": {MANAGED: "podgrove", ENVIRONMENT: self.ident}},
                    "spec": policy_spec(self.ident, self.settings, ingress=ingress, egress=egress)}
        _BoundedKube(self).reconcile_network_policy([resource], self.ident)

    def _withdraw(self):
        """Fenced by our controller, the environment labels and each object's UID/resourceVersion, not the Pod."""
        try:
            self._own_controller()
        except Exception as error:
            raise PodgroveError(f"withdrawal refused: {_reason(error)}") from error
        errors = []
        if self.settings.get("pod_to_pod") == "selected":
            try:
                self._apply()
            except Exception as error:  # every step is attempted whatever an earlier one raised
                errors.append(f"policy withdrawal failed: {_reason(error)}")
        try:
            self._publish(declaration(self.settings, self.name, [], None), self._own_lease())
        except Exception as error:
            errors.append(f"advertisement withdrawal failed: {_reason(error)}")
        if errors:
            raise PodgroveError("; ".join(errors))

    def refresh(self, rows=None, *, deadline=None, cancel_event=None):
        limit = time.monotonic() + RECONCILE_TIMEOUT
        deadline = min(limit, deadline) if deadline is not None else limit
        timeout = max(0, deadline - time.monotonic())
        if not self.reconciling.acquire(timeout=timeout):
            raise PodgroveError("Pod networking reconciliation is already running")
        try:
            return self._refresh(rows, deadline=deadline, cancel_event=cancel_event)
        finally:
            self.deadline = None
            self.cancel_event = self.stopping
            self.reconciling.release()

    def _refresh(self, rows, *, deadline=None, cancel_event=None):
        now = time.monotonic()
        deadline = min(now + RECONCILE_TIMEOUT, deadline) if deadline is not None else now + RECONCILE_TIMEOUT
        self.deadline = deadline - min(CLEANUP_TIMEOUT, max(0, deadline - now) / 3)
        self.cancel_event = self.stopping if cancel_event is None else _EitherCancelled(self.stopping, cancel_event)
        if rows is not None:
            with self.lock:
                self.rows = copy.deepcopy(rows)
        try:
            with self.lock:
                rows = copy.deepcopy(self.rows)
            ports = observed_ports(self.settings, self.model, rows)
            profile = declaration(self.settings, self.name, ports, self.expected_uids["pod_uid"])
            self._own_pod()
            self._publish(profile, self._own_lease())
            pending = []
            if self.settings.get("pod_to_pod") == "selected":
                peers, skipped = self._peers()
                ingress, egress, pending = selected_rules(self.kube.namespace, self.ident, profile, peers)
                pending = sorted({*pending, *skipped})
                self._own_pod()
                self._apply(ingress, egress)
            current = {"state": "waiting" if pending else "ready", "mode": self.settings.get("pod_to_pod", "disabled"),
                       "endpoints": addresses(self.kube.namespace, self.ident, ports), "pending": pending}
        except Exception as error:  # fail closed: any failure withdraws this engine's grants
            detail = _reason(error)
            if self.settings.get("pod_to_pod") in ("selected", "open"):
                try:
                    self.deadline = deadline
                    self.cancel_event = threading.Event()
                    self._withdraw()
                except Exception as revoke_error:
                    detail += f"; unable to withdraw peer access: {_reason(revoke_error)}"
            current = {"state": "unavailable", "mode": self.settings.get("pod_to_pod"), "endpoints": [], "error": detail}
        current["checked_at"] = time.time()
        self._record(current)
        return current

    def start(self, rows, *, deadline=None, cancel_event=None):
        def active():
            if (_EitherCancelled(self.stopping, cancel_event).is_set()
                    or deadline is not None and time.monotonic() >= deadline):
                raise PodgroveError("Pod networking startup cancelled or its deadline expired")
        active()
        if self.worker is not None:
            raise PodgroveError("Pod networking monitor already started")
        self.refresh(rows, deadline=deadline, cancel_event=cancel_event)
        active()
        def monitor():
            while not self.stopping.wait(INTERVAL):
                try:
                    self.refresh()
                except Exception as error:  # a refresh that cannot even start is reported, never silent
                    self._record({"state": "unavailable", "mode": self.settings.get("pod_to_pod"), "endpoints": [],
                                  "error": f"Pod networking refresh failed: {_reason(error)}", "checked_at": time.time()})
        self.worker = threading.Thread(target=monitor, name="podgrove-pod-network", daemon=True)
        self.worker.start()
        return self

    def update_rows(self, rows):
        with self.lock:
            self.rows = copy.deepcopy(rows)

    def _record(self, current):
        with self.lock:
            self.current = current
        if self.on_change:
            self.on_change(current)

    def snapshot(self):
        with self.lock:
            current = copy.deepcopy(self.current)
        if self.worker is not None and not self.worker.is_alive() and not self.stopping.is_set():
            current.update(state="unavailable", endpoints=[], error="Pod networking monitor stopped unexpectedly; peer grants are no longer reconciled")
        return current

    def close(self):
        self.stopping.set()
        if self.worker:
            self.worker.join(timeout=RECONCILE_TIMEOUT + 1)
            if self.worker.is_alive():
                raise PodgroveError("Pod networking monitor did not stop")
        if self.settings.get("pod_to_pod") in ("selected", "open"):
            deadline = time.monotonic() + CLEANUP_TIMEOUT
            if not self.reconciling.acquire(timeout=CLEANUP_TIMEOUT):
                raise PodgroveError("Pod networking reconciliation did not release cleanup ownership")
            try:
                self.deadline = deadline
                self.cancel_event = threading.Event()
                self._withdraw()
            finally:
                self.deadline = None
                self.cancel_event = self.stopping
                self.reconciling.release()
