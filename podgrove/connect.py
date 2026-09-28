"""Declared, same-namespace TCP links between independently owned engines."""
from __future__ import annotations

import copy
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.parse

from .docker_tunnel import DockerTunnel
from .errors import PodgroveError
from .kube import ENVIRONMENT, MANAGED, Kube

COMPONENT = "podgrove.dev/component"
TARGET_UID = "podgrove.dev/link-target-uid"
LINK = "podgrove.dev/link-name"


def validate_connectivity(config, model, ident):
    aliases = {f"{rule['name']}.podgrove" for rule in config.connect}
    if config.reverse:
        aliases.add("host.docker.internal")
    for rule in config.connect:
        if rule["environment"] == ident:
            raise PodgroveError("connect.environment must name another environment in this namespace")
    remote_ports = {rule["remote_port"] for rule in config.reverse}
    for name, service in model.get("services", {}).items():
        hosts = service.get("extra_hosts", {})
        if isinstance(hosts, list):
            hosts = dict(re.split(r"[=:]", entry, maxsplit=1) for entry in hosts)
        for alias in aliases & hosts.keys():
            if alias != "host.docker.internal" or hosts[alias] not in ("host-gateway", ["host-gateway"]):
                raise PodgroveError(f"services.{name}.extra_hosts conflicts with managed connectivity alias {alias}")
        for port in service.get("ports", []):
            published = str(port.get("published", "0"))
            bounds = [int(value) for value in published.split("-")]
            low, high = bounds[0], bounds[-1]
            if any(low <= remote <= high for remote in remote_ports):
                raise PodgroveError(f"services.{name}.ports conflicts with a reverse.remote_port listener")


def overlay_model(model, aliases):
    return {"services": {name: {"extra_hosts": aliases} for name in model["services"]}} if aliases else {}


def _owned(obj, namespace, ident, *, name=None):
    metadata = obj.get("metadata", {})
    labels = metadata.get("labels", {})
    if (metadata.get("namespace") != namespace or not metadata.get("uid")
            or not metadata.get("resourceVersion") or metadata.get("deletionTimestamp")
            or labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != ident
            or name is not None and metadata.get("name") != name):
        raise PodgroveError("Connectivity object ownership changed or is incomplete; refusing adoption")
    return metadata


def _docker_json(tunnel, path, *, cancelled=None, timeout=5):
    connection = http.client.HTTPConnection("127.0.0.1", tunnel.port, timeout=timeout)
    done = threading.Event()
    watcher = None
    deadline = time.monotonic() + timeout
    try:
        if cancelled is not None and cancelled.is_set():
            raise ValueError("cancelled Docker read")
        connection.connect()
        wire = connection.sock
        def cancel_read():
            while not done.wait(.02):
                if time.monotonic() >= deadline or cancelled is not None and cancelled.is_set():
                    try:
                        wire.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
        watcher = threading.Thread(target=cancel_read, name="podgrove-link-read", daemon=True)
        watcher.start()
        if cancelled is not None and cancelled.is_set():
            raise ValueError("cancelled Docker read")
        connection.request("GET", path)
        with connection.getresponse() as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if response.status != 200 or response.length not in (None, 0):
                raise ValueError("Docker read refused")
        if len(raw) > 4 * 1024 * 1024 or time.monotonic() >= deadline or cancelled is not None and cancelled.is_set():
            raise ValueError("oversized Docker response")
        return json.loads(raw)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise PodgroveError("Cannot inspect the declared connect service on its engine") from exc
    finally:
        done.set()
        connection.close()
        if watcher is not None:
            watcher.join(timeout=1)


def discover_endpoint(kube, rule):
    ident = rule["environment"]
    name = "pg-" + ident
    lease = kube.get("configmap", name)
    _owned(lease, kube.namespace, ident, name=name)
    project = lease.get("data", {}).get("compose_project", "")
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise PodgroveError("connect target has no recorded Compose project; run up there with this Podgrove release first")
    controller = _owned(kube.get("statefulset", name), kube.namespace, ident, name=name)
    tunnel = DockerTunnel(kube, ident, 0)
    try:
        tunnel.start()
        filters = json.dumps({"label": [f"com.docker.compose.project={project}", f"com.docker.compose.service={rule['service']}"]})
        cancelled = getattr(kube, "cancelled", None)
        rows = _docker_json(tunnel, "/containers/json?" + urllib.parse.urlencode({"filters": filters}), cancelled=cancelled)
        if not isinstance(rows, list) or len(rows) != 1:
            raise PodgroveError(f"connect {rule['name']}: expected exactly one running {rule['service']} container")
        container = rows[0]
        labels = container.get("Labels", {})
        cid = container.get("Id", "")
        if (not re.fullmatch(r"[0-9a-f]{64}", cid) or container.get("State") != "running"
                or labels.get("com.docker.compose.service") != rule["service"]
                or labels.get("com.docker.compose.project") != project
                or labels.get("com.docker.compose.oneoff", "false").lower() != "false"):
            raise PodgroveError("connect target is not an unambiguous running Compose service")
        inspected = _docker_json(tunnel, f"/containers/{cid}/json", cancelled=cancelled)
        inspected_labels = inspected.get("Config", {}).get("Labels", {})
        if (inspected.get("Id") != cid or inspected.get("State", {}).get("Running") is not True
                or inspected_labels.get("com.docker.compose.project") != project
                or inspected_labels.get("com.docker.compose.service") != rule["service"]
                or inspected_labels.get("com.docker.compose.oneoff", "false").lower() != "false"):
            raise PodgroveError("connect target changed during discovery")
        bindings = inspected.get("NetworkSettings", {}).get("Ports", {}).get(f"{rule['port']}/tcp") or []
        ports = {int(binding["HostPort"]) for binding in bindings
                 if binding.get("HostIp") == "0.0.0.0" and str(binding.get("HostPort", "")).isdigit()}
        if len(ports) != 1 or not 1 <= next(iter(ports)) <= 65535 or ports & {2375, 2376}:
            raise PodgroveError(f"connect {rule['name']}: publish {rule['service']}:{rule['port']} on 0.0.0.0 TCP first")
        tunnel.refresh_identity()
        expected = tunnel.identity_snapshot()["expected"]
        if expected["statefulset_uid"] != controller["uid"]:
            raise PodgroveError("connect target controller was replaced during discovery")
        return {**rule, "published": ports.pop(), "controller_uid": controller["uid"]}
    finally:
        tunnel.close()


def link_resources(namespace, ident, source_uid, endpoint):
    suffix = hashlib.sha256(endpoint["name"].encode()).hexdigest()[:10]
    name = f"pg-{ident}-link-{suffix}"
    labels = {MANAGED: "podgrove", ENVIRONMENT: ident, COMPONENT: "connection", LINK: endpoint["name"]}
    meta = {"namespace": namespace, "name": name, "labels": labels,
            "annotations": {TARGET_UID: endpoint["controller_uid"]},
            "ownerReferences": [{"apiVersion": "apps/v1", "kind": "StatefulSet", "name": "pg-" + ident,
                                 "uid": source_uid}]}
    source = {"matchLabels": {MANAGED: "podgrove", ENVIRONMENT: ident}}
    target = {"matchLabels": {MANAGED: "podgrove", ENVIRONMENT: endpoint["environment"]}}
    port = [{"protocol": "TCP", "port": endpoint["published"]}]
    service = {"apiVersion": "v1", "kind": "Service", "metadata": copy.deepcopy(meta),
               "spec": {"type": "ClusterIP", "selector": target["matchLabels"],
                        "ports": [{"name": "tcp", "port": endpoint["port"],
                                   "targetPort": endpoint["published"], "protocol": "TCP"}]}}
    egress = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": copy.deepcopy(meta),
              "spec": {"podSelector": source, "policyTypes": ["Egress"],
                       "egress": [{"to": [{"podSelector": target}], "ports": port}]}}
    ingress = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": copy.deepcopy(meta),
               "spec": {"podSelector": target, "policyTypes": ["Ingress"],
                        "ingress": [{"from": [{"podSelector": source}], "ports": port}]}}
    egress["metadata"]["name"] += "-out"
    ingress["metadata"]["name"] += "-in"
    return [service, egress, ingress]


class _EitherCancelled:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event is not None and event.is_set() for event in self.events)


class _CancellableKube(Kube):
    def __init__(self, kube, cancelled):
        super().__init__(kube.context, kube.namespace, namespace_mode=kube.namespace_mode)
        self.delegate, self.cancelled = kube, cancelled

    def call(self, *args, **kwargs):
        kwargs["cancel_event"] = _EitherCancelled(self.cancelled, kwargs.get("cancel_event"))
        return self.delegate.call(*args, **kwargs)


class EnvironmentLinks:
    def __init__(self, kube, ident, rules, *, interval=30):
        self._stop = threading.Event()
        self.kube, self.ident, self.rules = _CancellableKube(kube, self._stop), ident, rules
        self.interval = interval
        self.source_uid = None
        self.aliases = {}
        self.endpoints = {}
        self._state = {"state": "disabled" if not rules else "starting", "checked_at": time.time()}
        self._thread = None

    def _identity(self, obj):
        metadata = _owned(obj, self.kube.namespace, self.ident)
        if (metadata.get("labels", {}).get(COMPONENT) != "connection"
                or not re.fullmatch(rf"pg-{self.ident}-link-[0-9a-f]{{10}}(?:-in|-out)?", metadata.get("name", ""))
                or metadata.get("ownerReferences") != [{"apiVersion": "apps/v1", "kind": "StatefulSet",
                                                       "name": "pg-" + self.ident, "uid": self.source_uid}]):
            raise PodgroveError("Connection resource is foreign or has a different source controller")
        return metadata

    def _put(self, desired):
        existing = self.kube.get(desired["kind"], desired["metadata"]["name"])
        candidate = copy.deepcopy(desired)
        operation = "create"
        if existing:
            metadata = self._identity(existing)
            if metadata.get("annotations", {}).get(TARGET_UID) != desired["metadata"]["annotations"][TARGET_UID]:
                raise PodgroveError("connect target controller changed; remove this declaration and run up before relinking")
            candidate["metadata"].update(uid=metadata["uid"], resourceVersion=metadata["resourceVersion"])
            if desired["kind"] == "Service":
                for key in ("clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy"):
                    if key in existing["spec"]:
                        candidate["spec"][key] = existing["spec"][key]
                for key, default in (("sessionAffinity", "None"), ("internalTrafficPolicy", "Cluster")):
                    if existing["spec"].get(key) == default:
                        candidate["spec"][key] = default
            if (candidate["spec"] == existing.get("spec") and all(candidate["metadata"].get(key) == metadata.get(key)
                    for key in ("labels", "annotations", "ownerReferences"))):
                return existing
            operation = "replace"
        result = self.kube.call(operation, "-f", "-", "-o", "json", input=json.dumps(candidate))
        return json.loads(result.stdout)

    def _delete(self, obj):
        metadata = self._identity(obj)
        if obj["kind"] == "NetworkPolicy":
            api, resource = "/apis/networking.k8s.io/v1", "networkpolicies"
        elif obj["kind"] == "Service":
            api, resource = "/api/v1", "services"
        else:
            raise PodgroveError("Refusing deletion of unexpected connection resource kind")
        url = f"{api}/namespaces/{self.kube.namespace}/{resource}/{metadata['name']}"
        body = {"apiVersion": "v1", "kind": "DeleteOptions",
                "preconditions": {key: metadata[key] for key in ("uid", "resourceVersion")}}
        self.kube.call("delete", "--raw", url, "-f", "-", input=json.dumps(body))

    def _existing(self, kind):
        selector = f"{MANAGED}=podgrove,{ENVIRONMENT}={self.ident},{COMPONENT}=connection"
        return self.kube.get(kind, selector=selector).get("items", [])

    def _revoke(self, names=None):
        for obj in self._existing("NetworkPolicy"):
            if names is None or obj.get("metadata", {}).get("labels", {}).get(LINK) in names:
                self._delete(obj)

    def _reconcile(self):
        source = _owned(self.kube.get("statefulset", "pg-" + self.ident), self.kube.namespace,
                        self.ident, name="pg-" + self.ident)
        if self.source_uid is not None and self.source_uid != source["uid"]:
            raise PodgroveError("Source engine controller changed; refusing connectivity mutations")
        self.source_uid = source["uid"]
        endpoints = {rule["name"]: discover_endpoint(self.kube, rule) for rule in self.rules}
        wanted = set(endpoints)
        for obj in self._existing("NetworkPolicy") + self._existing("Service"):
            if obj.get("metadata", {}).get("labels", {}).get(LINK) not in wanted:
                self._delete(obj)
        aliases = {}
        for name, endpoint in endpoints.items():
            if name in self.endpoints and self.endpoints[name] != endpoint:
                self._revoke({name})
            resources = link_resources(self.kube.namespace, self.ident, self.source_uid, endpoint)
            service = self._put(resources[0])
            address = service.get("spec", {}).get("clusterIP", "")
            try:
                ipaddress.ip_address(address)
            except ValueError as exc:
                raise PodgroveError("Connection Service did not receive a usable ClusterIP") from exc
            alias = name + ".podgrove"
            if alias in self.aliases and self.aliases[alias] != address:
                raise PodgroveError("Connection Service IP changed; run up to recreate service host mappings")
            aliases[alias] = address
            for resource in resources[1:]:
                self._put(resource)
        self.aliases, self.endpoints = aliases, endpoints
        self._state = {"state": "ready" if endpoints else "disabled", "checked_at": time.time(),
                       "endpoints": [{"name": f"{name}.podgrove", "environment": value["environment"],
                                      "service": value["service"], "port": value["port"]}
                                     for name, value in endpoints.items()]}

    def start(self):
        try:
            self._reconcile()
        except BaseException:
            if self.source_uid is not None:
                self._revoke()
            raise
        if self.rules:
            self._thread = threading.Thread(target=self._monitor, name="podgrove-connect", daemon=True)
            self._thread.start()
        return self

    def _monitor(self):
        while not self._stop.wait(self.interval):
            try:
                self._reconcile()
            except Exception as exc:
                if self._stop.is_set():
                    return
                detail = str(exc)
                try:
                    self._revoke()
                except Exception as revoke_error:
                    detail += f"; cannot revoke connection policies: {revoke_error}"
                self._state = {"state": "disconnected", "checked_at": time.time(), "error": detail}

    def snapshot(self):
        return copy.deepcopy(self._state)

    def check(self):
        return self.snapshot()

    def cancel(self):
        self._stop.set()

    def close(self):
        self.cancel()
        if self._thread:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise PodgroveError("Connection monitor has not stopped; refusing a concurrent replacement")
