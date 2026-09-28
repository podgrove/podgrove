"""Loopback-only, read-only dashboard for locally recorded environments."""
from __future__ import annotations

from collections import Counter
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import jsonschema
import yaml

from . import state
from .config import CONFIG_SCHEMA, _UniqueLoader, default_tainted_nodes
from .errors import PodgroveError
from .kube import ENVIRONMENT, MANAGED, Kube, context_name, engine_pod_name, namespace_name, validate_tainted_nodes
from .network import network_settings
from .runtime import control
from .session_status import observed
from .sync_filter import validate_patterns
from .resources import RESOURCE_NAMES, engine_resources, initializer_resources, quantity_text
from .repository import repository_labels

MAX_JSON = 1024 * 1024
MAX_LOG = 64 * 1024
MAX_CONFIG = 64 * 1024
MAX_CONFIG_NODES = 2048
MAX_CONFIG_DEPTH = 16
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
IDENTITY = re.compile(r"[a-f0-9]{12}")
STATIC = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
          "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"),
          "/tokens.css": ("tokens.css", "text/css"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}


class WebError(Exception):
    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


def _validate_log_tail(tail: int | str) -> int | str:
    if tail == "all" or (type(tail) is int and 1 <= tail <= 200):
        return tail
    raise WebError("Select a tail between 1 and 200, or all retained logs", 400)


def _parse_log_tail(value: str) -> int | str:
    if value == "all":
        return value
    if not re.fullmatch(r"[0-9]{1,3}", value):
        raise WebError("Invalid log tail", 400)
    return _validate_log_tail(int(value))


def _stop(process: subprocess.Popen) -> None:
    try:
        # Still signal when the leader exited: an authentication helper may
        # remain in its private group and keep our capture pipes open.
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can report EPERM while an exited leader awaits reaping. Only
        # dismiss that race after confirming exit; a live denial is an error.
        if process.poll() is None:
            raise
    process.wait(timeout=2)


def bounded_read_command(args: list[str], *, timeout: float = 6, limit: int = MAX_JSON,
                         cancel: threading.Event | None = None) -> bytes:
    """Bound both stdout/stderr allocation and lifetime, without invoking a shell."""
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        raise WebError("Required read-only CLI is unavailable") from exc
    output = bytearray()
    stderr_size = 0
    deadline = time.monotonic() + timeout
    completed = False
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(process.stdout, selectors.EVENT_READ, "stdout")
            poll.register(process.stderr, selectors.EVENT_READ, "stderr")
            while poll.get_map():
                if cancel is not None and cancel.is_set():
                    raise WebError("Dashboard read was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WebError("Cluster read timed out")
                for key, _ in poll.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, 16384)
                    if not chunk:
                        poll.unregister(key.fileobj)
                        continue
                    if key.data == "stdout":
                        output.extend(chunk)
                    else:
                        stderr_size += len(chunk)
                    if len(output) > limit or stderr_size > 16384:
                        raise WebError("Cluster response exceeded the dashboard size limit")
        code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        completed = True
        if code:
            # Never return kubectl/AWS stderr or command arguments to the browser.
            raise WebError("Cluster read failed; check connectivity and read permissions")
        return bytes(output)
    except subprocess.TimeoutExpired as exc:
        raise WebError("Cluster read timed out") from exc
    finally:
        primary_error = sys.exception()
        try:
            if not completed:
                _stop(process)
        except (OSError, subprocess.TimeoutExpired) as cleanup_error:
            if primary_error is None:
                raise WebError("Dashboard command cleanup could not be confirmed") from cleanup_error
            primary_error.add_note("Dashboard command cleanup could not be confirmed")
        finally:
            process.stdout.close()
            process.stderr.close()


def _text(value, limit: int = 256) -> str:
    return str(value)[:limit] if isinstance(value, (str, int, float)) else ""


def _integer(value, default=0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


class _MetadataLoader(_UniqueLoader):
    """Bound YAML work before construction; never expand aliases or custom tags."""
    def __init__(self, stream):
        super().__init__(stream)
        self.nodes = self.depth = 0

    def compose_node(self, parent, index):
        self.nodes += 1
        self.depth += 1
        try:
            if (self.nodes > MAX_CONFIG_NODES or self.depth > MAX_CONFIG_DEPTH
                    or self.check_event(yaml.AliasEvent)):
                raise yaml.YAMLError("Configuration exceeds safe metadata parsing limits")
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1


def _relative_config_path(root: Path, value: str) -> str:
    """Lexical normalization only: referenced Compose/env paths are never opened."""
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ValueError("Invalid metadata path")
    candidate = Path(value)
    if ".." in candidate.parts or ".git" in candidate.parts:
        raise ValueError("Unsafe metadata path")
    if candidate.is_absolute():
        candidate = candidate.relative_to(root)
    return str(candidate)


def _configuration_settings(raw: bytes, root: Path) -> dict:
    loader = _MetadataLoader(raw.decode("utf-8"))
    try:
        data = loader.get_single_data()
    finally:
        loader.dispose()
    if data is None:
        data = {}
    if not jsonschema.Draft202012Validator(CONFIG_SCHEMA).is_valid(data):
        raise ValueError("Invalid configuration")
    cluster = data.get("cluster", {})
    target = {"context": context_name(cluster["context"]) if "context" in cluster else None,
              "namespace": namespace_name(cluster["namespace"]) if "namespace" in cluster else None,
              "namespace_mode": cluster.get("namespace_mode", "shared"),
              "storage_class": cluster.get("storage_class")}
    duration = re.fullmatch(r"([1-9][0-9]*)([smhd])", data.get("ttl", "8h"))
    ttl = int(duration[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[duration[2]]
    if ttl > 2**53:
        raise ValueError("Invalid TTL")
    placement = default_tainted_nodes()
    configured = data.get("tainted_nodes", {})
    placement["selector"] = configured.get("selector", placement["selector"])
    placement["taint"].update(configured.get("taint", {}))
    validate_tainted_nodes(placement)
    compose = data.get("compose", {})
    files = ([_relative_config_path(root, name) for name in compose["files"]]
             if "files" in compose else None)
    profiles = compose.get("profiles", [])
    forwards = data.get("forward")
    if any(not NAME.fullmatch(profile) for profile in profiles):
        raise ValueError("Invalid profile")
    if forwards is not None:
        if (any(not NAME.fullmatch(entry["service"]) for entry in forwards)
                or len({(entry["service"], entry["port"]) for entry in forwards}) != len(forwards)):
            raise ValueError("Invalid forward target")
        local_ports = [entry["local"] for entry in forwards if "local" in entry]
        if len(set(local_ports)) != len(local_ports):
            raise ValueError("Duplicate local forward")
    return {"version": 1, "cluster": target, "size": data.get("size", "medium"), "node_mode": data.get("node_mode", "shared"),
            "resources_mode": "custom" if "resources" in data else "preset",
            "resources": engine_resources(data.get("size", "medium"), data.get("resources")),
            "init_resources": initializer_resources(data.get("init_resources")),
            "storage": {"size": quantity_text(data.get("storage", {}).get("size", "20Gi"), "storage.size")},
            "sync": {"exclude": validate_patterns(data.get("sync", {}).get("exclude", []))},
            "network": network_settings(data.get("network")),
            "tainted_nodes": placement if data.get("node_mode") == "tainted" else None,
            "ttl_seconds": ttl, "compose": {"files": files, "profiles": profiles,
                                               "project_directory": _relative_config_path(root, compose.get("project_directory", "."))},
            "forward": forwards}


def configuration_metadata(data: dict) -> dict:
    """Read the current config through pinned no-follow descriptors, without applying it."""
    result = {"source": "current_file", "status": "unavailable", "file": None, "settings": None,
              "warning": "Current configuration is unavailable or invalid; saved environment settings are unchanged."}
    descriptors = []
    try:
        root = state.configuration_root(data)
        if not root.is_absolute() or ".." in root.parts or ".git" in root.parts or len(str(root)) > 4096:
            return result
        selected = data.get("config_path")
        if selected is None:
            selected = "podgrove.yml"
        relative = _relative_config_path(root, selected)
        result["file"] = relative
        parts = Path(relative).parts
        if not parts:
            return result
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory = os.open(root.anchor, flags)
        descriptors.append(directory)
        for component in root.parts[1:]:
            child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = descriptors[-1] = child
        if os.fstat(directory).st_uid != os.getuid():
            return result
        try:
            for component in parts[:-1]:
                child = os.open(component, flags, dir_fd=directory)
                os.close(directory)
                directory = descriptors[-1] = child
            file = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                           dir_fd=directory)
            descriptors.append(file)
        except FileNotFoundError:
            result.update(status="missing", warning="No current configuration file exists; saved environment settings are unchanged.")
            return result
        before = os.fstat(file)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_size > MAX_CONFIG):
            return result
        raw = bytearray()
        while len(raw) <= MAX_CONFIG:
            chunk = os.read(file, min(16384, MAX_CONFIG + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(file)
        if (len(raw) > MAX_CONFIG or len(raw) != before.st_size
                or (before.st_mtime_ns, before.st_ctime_ns, before.st_size, before.st_nlink)
                != (after.st_mtime_ns, after.st_ctime_ns, after.st_size, after.st_nlink)):
            return result
        result.update(status="available", settings=_configuration_settings(bytes(raw), root), warning=None)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, yaml.YAMLError, PodgroveError, RecursionError):
        pass  # Never disclose YAML contents, unsafe external paths, or raw loader/OS errors.
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return result


def _ports(data: dict) -> list[dict]:
    result = []
    ports = data.get("ports", [])
    for port in (ports if isinstance(ports, list) else [])[:256]:
        if (isinstance(port, dict) and isinstance(port.get("service"), str) and NAME.fullmatch(port["service"])
                and 1 <= _integer(port.get("local")) <= 65535 and 1 <= _integer(port.get("target")) <= 65535):
            result.append({"service": port["service"], "target": port["target"], "local": port["local"],
                           "status": port.get("status") if port.get("status") in ("ready", "reconnecting", "disconnected") else "unknown",
                           "url": f"http://127.0.0.1:{port['local']}"})
    return result


def _services(rows: list[dict], ports: list[dict]) -> tuple[list[dict], dict]:
    grouped = {}
    counts = Counter(total=0, running=0, healthy=0, unhealthy=0, exited=0)
    for row in (rows if isinstance(rows, list) else [])[:256]:
        if not isinstance(row, dict):
            continue
        name = row.get("Service", "")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            continue
        container = {"id": _text(row.get("ID"), 64), "name": _text(row.get("Name")),
                     "state": _text(row.get("State"), 32), "health": _text(row.get("Health"), 32),
                     "image": _text(row.get("Image"), 256)}
        grouped.setdefault(name, []).append(container)
        counts["total"] += 1
        for key, value in (("running", container["state"]), ("exited", container["state"]),
                              ("healthy", container["health"]), ("unhealthy", container["health"])):
            counts[key] += value == key
    services = []
    for name, containers in sorted(grouped.items()):
        states, health = ({row[field] for row in containers} for field in ("state", "health"))
        services.append({"name": name, "state": next(iter(states)) if len(states) == 1 else "mixed",
                         "health": next(iter(health)) if len(health) == 1 else "mixed",
                         "image": containers[0]["image"], "replicas": len(containers), "containers": containers,
                         "ports": [port for port in ports if port["service"] == name]})
    return services, dict(counts)


def _redact_logs(text: str) -> str:
    text = re.sub(r"(?i)\b((?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r'''(?i)((?:["']?)(?:password|passwd|secret|token|api[_-]?key|authorization|access[_-]?key|docker_host)(?:["']?)\s*[:=]\s*)(?:["'][^"'\r\n]*["']|[^\s,;]+)''',
                  r"\1[REDACTED]", text)
    return text


def _sync_status(data: dict) -> dict:
    """Expose recovery diagnostics without copying private runtime metadata."""
    raw = data.get("sync_status")
    raw = raw if isinstance(raw, dict) else {}
    current = raw.get("state")
    if current not in ("ready", "retrying", "reconnecting", "disconnected", "disabled"):
        current = "unknown"
    if data.get("status") in ("disconnected", "error", "reaped"):
        current = "disconnected"
    # Redact before clipping: cutting off a closing quote can turn one secret
    # containing spaces into several apparently unrelated words.
    error = raw.get("error") if isinstance(raw.get("error"), str) else ""
    result = {"state": current, "attempts": _integer(raw.get("attempts")),
              "error": _redact_logs(error)[:1024]}
    for key in ("checked_at", "next_retry_at"):
        value = raw.get(key)
        result[key] = value if type(value) in (int, float) and 0 <= value <= 253402300799 else None
    return result


def _docker_log_text(raw: bytes) -> str:
    # Non-TTY Docker logs use an eight-byte stream header. Return complete or
    # truncated payloads, never their binary framing. TTY logs are plain text.
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\0\0\0":
        chunks, offset = [], 0
        while offset + 8 <= len(raw):
            if raw[offset] not in (0, 1, 2) or raw[offset + 1:offset + 4] != b"\0\0\0":
                break
            size = int.from_bytes(raw[offset + 4:offset + 8], "big")
            chunks.append(raw[offset + 8:offset + 8 + size])
            offset += 8 + size
        raw = b"".join(chunks)
    return _redact_logs(raw.decode("utf-8", errors="replace"))


class Dashboard:
    def __init__(self, context: str, namespace: str | None = None):
        if not context:
            raise PodgroveError("web requires --context, PODGROVE_CONTEXT, or cluster.context in podgrove.yml")
        context = context_name(context)
        if namespace is not None:
            Kube(context, namespace)  # Validate namespace syntax; no API call.
        self.context, self.namespace = context, namespace

    def _records(self) -> list[dict]:
        return state.local_records(self.context, self.namespace)

    def _record(self, ident: str) -> dict:
        if not IDENTITY.fullmatch(ident):
            raise WebError("Unknown environment", 404)
        for entry in self._records():
            if entry.get("data", {}).get("identity") == ident:
                return entry["data"]
        raise WebError("Unknown environment", 404)

    @staticmethod
    def _summary(data: dict) -> dict:
        data = observed(data)
        metadata = repository_labels(Path(data["root"]), environ={})
        ports = _ports(data)
        services, counts = _services(data.get("services", []), ports)
        try:
            network = network_settings(data["network"]) if isinstance(data.get("network"), dict) else None
        except PodgroveError:
            network = None
        omitted_ports = max(0, len(data["ports"]) - 256) if isinstance(data.get("ports"), list) else 0
        omitted_services = max(0, len(data["services"]) - 256) if isinstance(data.get("services"), list) else 0
        return {"identity": data["identity"], "name": Path(data["root"]).name, "root": data["root"],
                "worktree": Path(data["root"]).name, "repository": metadata["repo"],
                "branch": metadata["branch"] if metadata["branch"] != "unspecified" else None,
                "context": _text(data.get("context"), 512),
                "namespace": data["namespace"], "status": _text(data.get("status", "unknown"), 32),
                "namespace_mode": data.get("namespace_mode") if data.get("namespace_mode") in ("shared", "worktree", "exclusive") else None,
                "node_mode": _text(data.get("node_mode", "unknown"), 32),
                "network": network,
                "created_at": data.get("created_at") if isinstance(data.get("created_at"), (int, float)) else None,
                "last_activity": data.get("last_activity") if isinstance(data.get("last_activity"), (int, float)) else None,
                "ttl_seconds": _integer(data.get("ttl_seconds")), "ports": ports, "services": services, "counts": counts,
                "forward_status": {"state": data["forward_status"]["state"]},
                "sync_status": _sync_status(data),
                "ports_truncated": bool(omitted_ports), "ports_omitted": omitted_ports,
                "services_truncated": bool(omitted_services), "service_observations_omitted": omitted_services,
                "source": "local_snapshot", "health_fresh": False, "health_observed_at": None,
                "observed_at": time.time()}

    def environments(self) -> dict:
        rows, errors = [], []
        for entry in self._records():
            if "data" in entry:
                rows.append(self._summary(entry["data"]))
            else:
                errors.append({"identity": entry["path"].name.split("-")[0], "error": "Local environment state is invalid"})
        return {"context": self.context, "environments": rows, "errors": errors, "observed_at": time.time()}

    def settings(self, namespace: str | None = None) -> dict:
        from .web_settings import settings
        options = ([self.namespace] if self.namespace is not None else
                   sorted({entry["data"]["namespace"] for entry in self._records() if "data" in entry}))
        return settings(self.context, options, namespace=namespace)

    @staticmethod
    def _kube_read(kube: Kube, kind: str, name: str, *, cancel: threading.Event | None = None) -> dict:
        try:
            options = {"cancel": cancel} if cancel is not None else {}
            raw = bounded_read_command(kube.command("get", kind, name, "-o", "json", "--ignore-not-found"), **options)
            result = json.loads(raw) if raw.strip() else {}
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, TypeError) as exc:
            raise WebError("Cluster returned invalid metadata") from exc

    def _engine(self, data: dict, *, cancel: threading.Event | None = None) -> tuple[Kube, dict, dict, dict]:
        ident, namespace = data["identity"], data["namespace"]
        kube = Kube(self.context, namespace)
        resources = []
        for kind, name in (("statefulset", "pg-" + ident), ("pod", engine_pod_name(ident)),
                           ("persistentvolumeclaim", "pg-" + ident)):
            resource = self._kube_read(kube, kind, name, **({"cancel": cancel} if cancel is not None else {}))
            meta = resource.get("metadata", {})
            labels = meta.get("labels", {})
            if (meta.get("name") != name or meta.get("namespace") != namespace or not meta.get("uid")
                    or meta.get("deletionTimestamp") or labels.get(MANAGED) != "podgrove"
                    or labels.get(ENVIRONMENT) != ident):
                raise WebError("Owned engine resources are missing, terminating, or changed")
            resources.append(resource)
        controller, pod, pvc = resources
        try:
            Kube._validate_pod_controller(pod, controller, ident)
        except PodgroveError as exc:
            raise WebError("Engine ownership could not be verified") from exc
        claims = [volume["persistentVolumeClaim"].get("claimName")
                  for volume in pod.get("spec", {}).get("volumes", []) if "persistentVolumeClaim" in volume]
        if claims != ["pg-" + ident]:
            raise WebError("Engine storage ownership could not be verified")
        return kube, controller, pod, pvc

    @staticmethod
    def _docker_connection(data: dict, *, timeout: float = 6) -> http.client.HTTPConnection:
        deadline = time.monotonic() + timeout
        host = data.get("docker_host", "")
        match = re.fullmatch(r"tcp://127\.0\.0\.1:([0-9]{1,5})", host) if isinstance(host, str) else None
        token = data.get("token", "")
        if (not match or not 1 <= int(match[1]) <= 65535 or not isinstance(token, str)
                or not re.fullmatch(r"[a-f0-9]{32}", token)):
            raise WebError("Environment has no authenticated local Docker connection")
        expected_socket = Path(tempfile.gettempdir()) / f"podgrove-{os.getuid()}-{token[:16]}.sock"
        try:
            if data.get("socket") != str(expected_socket):
                raise ValueError()
            metadata = expected_socket.lstat()
            if metadata.st_uid != os.getuid() or not stat.S_ISSOCK(metadata.st_mode):
                raise ValueError()
            if control(data, "ping", timeout=min(timeout, 1)).get("ok") is not True:
                raise ValueError()
        except (OSError, ValueError, PodgroveError) as exc:
            raise WebError("Environment session is disconnected") from exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebError("Docker read timed out")
        connection = http.client.HTTPConnection("127.0.0.1", int(match[1]), timeout=remaining)
        try:
            connection.connect()
        except OSError as exc:
            connection.close()
            raise WebError("Docker read is unavailable or timed out") from exc
        return connection

    @staticmethod
    def _docker(data: dict, path: str, *, limit: int = MAX_JSON, allow_truncated: bool = False,
                timeout: float = 6) -> tuple[bytes, bool]:
        deadline = time.monotonic() + timeout
        connection = Dashboard._docker_connection(data, timeout=timeout)
        watchdog = None
        response = None
        try:
            transport = connection.sock
            def expire():
                # The watchdog also bounds trickling HTTP headers; individual
                # socket timeouts alone only bound inactivity.
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            watchdog = threading.Timer(max(0, deadline - time.monotonic()), expire)
            watchdog.start()
            connection.request("GET", path, headers={"Connection": "close"})
            response = connection.getresponse()
            if response.status != 200:
                raise WebError("Docker read failed; the environment may have changed")
            body = bytearray()
            while len(body) <= limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WebError("Docker read timed out")
                # read1 closes a fully consumed Content-Length response's
                # socket. Do not touch it again merely to discover EOF.
                if response.length == 0:
                    break
                transport.settimeout(remaining)
                chunk = response.read1(min(16384, limit + 1 - len(body)))
                if not chunk:
                    if time.monotonic() >= deadline:
                        raise WebError("Docker read timed out")
                    if response.length is not None and response.length > 0:
                        raise WebError("Docker response ended before its declared length")
                    break
                body.extend(chunk)
            truncated = len(body) > limit
            if truncated and not allow_truncated:
                raise WebError("Docker response exceeded the dashboard size limit")
            return bytes(body[:limit]), truncated
        except (OSError, http.client.HTTPException) as exc:
            raise WebError("Docker read is unavailable or timed out") from exc
        finally:
            if watchdog is not None:
                watchdog.cancel()
                watchdog.join(timeout=1)
            if response is not None:
                response.close()
            connection.close()

    def _containers(self, data: dict) -> list[dict]:
        query = urlencode({"all": "1", "filters": json.dumps({"label": ["com.docker.compose.project"]})})
        raw, _ = self._docker(data, "/containers/json?" + query)
        try:
            rows = json.loads(raw)
            if not isinstance(rows, list) or len(rows) > 256:
                raise ValueError()
            for row in rows:
                labels = row.get("Labels", {})
                if (not isinstance(row.get("Id"), str) or not re.fullmatch(r"[a-f0-9]{64}", row["Id"])
                        or not isinstance(labels.get("com.docker.compose.project"), str)
                        or not NAME.fullmatch(labels.get("com.docker.compose.service", ""))):
                    raise ValueError()
            return rows
        except (ValueError, TypeError, AttributeError) as exc:
            raise WebError("Docker returned invalid Compose container metadata") from exc

    def detail(self, ident: str) -> dict:
        data = self._record(ident)
        result = self._summary(data)
        result.update(engine=None, storage=None, warnings=[], configuration=configuration_metadata(data))
        try:
            ping = control(data, "ping", timeout=1)
            health = observed(data, connected=isinstance(ping, dict) and ping.get("ok") is True, ping=ping)
        except PodgroveError:
            health = observed(data, connected=False)
        result.update(status=health["status"], ports=_ports(health),
                      forward_status={"state": health["forward_status"]["state"]},
                      sync_status=_sync_status(health))
        try:
            _, controller, pod, pvc = self._engine(data)
            status, spec = pod.get("status", {}), pod.get("spec", {})
            container = next((row for row in spec.get("containers", []) if row.get("name") == "docker"), {})
            resources = {key: {field: _text(value.get(field)) for field in RESOURCE_NAMES if field in value}
                         for key, value in container.get("resources", {}).items() if key in ("requests", "limits")}
            result["engine"] = {
                "statefulset": {"name": controller["metadata"]["name"], "uid": controller["metadata"]["uid"],
                                "replicas": _integer(controller.get("spec", {}).get("replicas")),
                                "ready_replicas": _integer(controller.get("status", {}).get("readyReplicas"))},
                "pod": {"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                        "phase": _text(status.get("phase")), "node": _text(spec.get("nodeName")),
                        "ready": any(row.get("type") == "Ready" and row.get("status") == "True"
                                     for row in status.get("conditions", [])),
                        "restarts": sum(_integer(row.get("restartCount")) for row in status.get("containerStatuses", [])),
                        "resources": resources},
                "init_containers": [{"name": _text(row.get("name")), "resources": {
                    key: {field: _text(value.get(field)) for field in RESOURCE_NAMES if field in value}
                    for key, value in row.get("resources", {}).items() if key in ("requests", "limits")}}
                    for row in spec.get("initContainers", []) if row.get("name") == "storage"]}
            result["storage"] = {"name": pvc["metadata"]["name"], "uid": pvc["metadata"]["uid"],
                                 "phase": _text(pvc.get("status", {}).get("phase")),
                                 "requested": _text(pvc.get("spec", {}).get("resources", {}).get("requests", {}).get("storage")),
                                 "capacity": _text(pvc.get("status", {}).get("capacity", {}).get("storage")),
                                 "storage_class": _text(pvc.get("spec", {}).get("storageClassName")),
                                 "volume": _text(pvc.get("spec", {}).get("volumeName"))}
            containers = self._containers(data)
            rows = []
            for container in containers:
                status = container.get("Status", "")
                health = "unhealthy" if "(unhealthy)" in status else "healthy" if "(healthy)" in status else "starting" if "health: starting" in status else ""
                rows.append({"Service": container["Labels"]["com.docker.compose.service"], "ID": container["Id"],
                             "Name": (container.get("Names") or [""])[0].lstrip("/"), "State": container.get("State"),
                             "Health": health, "Image": container.get("Image")})
            result["services"], result["counts"] = _services(rows, result["ports"])
            result.update(source="live", health_fresh=True, health_observed_at=time.time(),
                          services_truncated=False, service_observations_omitted=0)
        except WebError as exc:
            result["warnings"].append(str(exc))
        return result

    def logs(self, ident: str, *, source: str, service: str | None, tail: int | str,
             container: str | None = None) -> dict:
        _validate_log_tail(tail)
        if source not in ("engine", "service"):
            raise WebError("Select engine or service logs", 400)
        if source == "service" and (not isinstance(service, str) or not NAME.fullmatch(service)):
            raise WebError("Select an existing Compose service", 400)
        if source == "engine" and (service is not None or container is not None):
            raise WebError("Engine logs do not accept service or container parameters", 400)
        if container is not None and (not isinstance(container, str) or not re.fullmatch(r"[a-f0-9]{64}", container)):
            raise WebError("Select a valid service container", 400)
        data = self._record(ident)
        kube, _, pod, _ = self._engine(data)
        if source == "engine":
            observed_uid = pod["metadata"]["uid"]
            raw = bounded_read_command(kube.command("logs", "pod/" + pod["metadata"]["name"], "--container", "docker",
                                                     "--tail", "-1" if tail == "all" else str(tail), "--limit-bytes", str(MAX_LOG), "--timestamps=true"),
                                       limit=MAX_LOG)
            current = self._kube_read(kube, "pod", pod["metadata"]["name"])
            meta = current.get("metadata", {})
            labels = meta.get("labels", {})
            if (meta.get("uid") != observed_uid or meta.get("namespace") != data["namespace"]
                    or labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != ident):
                raise WebError("Engine changed during the log read; retry after reconnecting")
            text, truncated = _redact_logs(raw.decode("utf-8", errors="replace")), len(raw) >= MAX_LOG
        else:
            containers = [row for row in self._containers(data)
                          if row["Labels"]["com.docker.compose.service"] == service]
            if not containers:
                raise WebError("Unknown Compose service", 404)
            if len({row["Labels"]["com.docker.compose.project"] for row in containers}) != 1:
                raise WebError("Service name is ambiguous across Compose projects", 409)
            if container is not None:
                containers = [row for row in containers if row["Id"] == container]
                if not containers:
                    raise WebError("Unknown Compose service container", 404)
            chunks, remaining, truncated = [], MAX_LOG, False
            deadline = time.monotonic() + 15
            for container in containers[:16]:
                available = deadline - time.monotonic()
                if available <= 0:
                    truncated = True
                    break
                query = urlencode({"stdout": 1, "stderr": 1, "timestamps": 1, "tail": tail})
                raw, clipped = self._docker(data, f"/containers/{container['Id']}/logs?{query}",
                                            limit=remaining, allow_truncated=True, timeout=min(6, available))
                chunks.append(_docker_log_text(raw))
                remaining -= len(raw)
                truncated |= clipped
                if remaining <= 0:
                    truncated = True
                    break
            truncated |= len(containers) > 16
            text = "\n".join(chunks)
        return {"source": source, "service": service, "text": text, "truncated": truncated,
                "tail": tail, "observed_at": time.time()}

    def logs_stream(self, ident: str, *, source: str, service: str | None, tail: int | str,
                    container: str | None = None):
        from .web_logs import LogStream
        return LogStream(self, ident, source=source, service=service, tail=tail, container=container)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, context: str, port: int = 0, *, namespace: str | None = None,
                 backend: Dashboard | None = None, static_dir: Path | None = None):
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise PodgroveError("--port must be between zero and 65535")
        self.backend = backend or Dashboard(context, namespace)
        self.token = secrets.token_urlsafe(32)
        self.static_dir = static_dir or Path(__file__).with_name("web_static")
        self.request_slots = threading.BoundedSemaphore(4)
        self.stream_slots = threading.BoundedSemaphore(2)
        self.stream_lock = threading.Lock()
        self.active_streams = set()
        self.stopping = threading.Event()
        self.connection_slots = threading.BoundedSemaphore(8)
        super().__init__(("127.0.0.1", port), DashboardHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    @property
    def url(self) -> str:
        return self.origin + "/#token=" + self.token

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()

    def server_close(self):
        self.stopping.set()
        with self.stream_lock:
            streams = list(self.active_streams)
        for stream in streams:
            stream.close("server_shutdown")
        super().server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "Podgrove"
    sys_version = ""

    def log_message(self, *_args):
        pass  # No token, query string, environment path, or application log access log.

    def _respond(self, status: int, data: bytes, content_type: str = "application/json") -> None:
        if len(data) > MAX_JSON:
            status, data, content_type = 503, b'{"error":"Response exceeded the dashboard size limit"}', "application/json"
        self._headers(status, content_type, len(data))
        self.wfile.write(data)
        self.close_connection = True

    def _headers(self, status: int, content_type: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        self.end_headers()

    def _json(self, status: int, data: dict) -> None:
        self._respond(status, json.dumps(data, allow_nan=False).encode())

    def _stream(self, ident: str, query: dict) -> None:
        if not set(query) <= {"source", "service", "tail", "container"}:
            raise WebError("Unknown log parameter", 400)
        tail = _parse_log_tail(query.get("tail", ["100"])[0])
        if not self.server.stream_slots.acquire(blocking=False):
            raise WebError("Two log streams are already open; pause one before starting another", 429)
        stream = None
        watcher = None
        watch_stopped = threading.Event()
        try:
            stream = self.server.backend.logs_stream(
                ident, source=query.get("source", ["engine"])[0], service=query.get("service", [None])[0],
                tail=tail, container=query.get("container", [None])[0])
            with self.server.stream_lock:
                if self.server.stopping.is_set():
                    raise WebError("Dashboard is stopping")
                self.server.active_streams.add(stream)

            def disconnected():
                # Stream requests accept no further input. Read readiness means
                # FIN/RST or unexpected pipelined bytes; either cancels this one
                # follower, including any in-flight ownership subprocess read.
                try:
                    with selectors.DefaultSelector() as poll:
                        poll.register(self.connection, selectors.EVENT_READ)
                        while not watch_stopped.is_set() and not stream.stopped.is_set():
                            if poll.select(0.1):
                                stream.close("client_cancelled")
                                return
                except (OSError, ValueError):
                    stream.close("client_cancelled")

            watcher = threading.Thread(target=disconnected, name="podgrove-web-log-client", daemon=True)
            watcher.start()
            initial = stream.start()
            self._headers(200, "application/x-ndjson")
            self.close_connection = True
            try:
                self.wfile.write(json.dumps(initial, allow_nan=False).encode() + b"\n")
                self.wfile.flush()
                for record in stream.events():
                    self.wfile.write(json.dumps(record, allow_nan=False).encode() + b"\n")
                    self.wfile.flush()
            except (OSError, ValueError):
                pass  # A cancelled/slow client must not trigger a second HTTP response.
        finally:
            watch_stopped.set()
            if watcher is not None:
                watcher.join(timeout=1)
            if stream is not None:
                stream.close()
                with self.server.stream_lock:
                    self.server.active_streams.discard(stream)
            self.server.stream_slots.release()

    def do_GET(self):
        try:
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            if hosts != [urlsplit(self.server.origin).netloc] or (origins and origins != [self.server.origin]):
                raise WebError("Host or Origin is not permitted", 403)
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise WebError("Cross-site requests are not permitted", 403)
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                raise WebError("Read-only requests must not include a body", 400)
            target = urlsplit(self.path)
            if target.scheme or target.netloc or target.fragment:
                raise WebError("Invalid request target", 400)
            if target.path in STATIC:
                if target.query:
                    raise WebError("Static files do not accept parameters", 400)
                name, content_type = STATIC[target.path]
                path = self.server.static_dir / name
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON:
                    raise WebError("Dashboard asset is unavailable", 404)
                self._respond(200, path.read_bytes(), content_type)
                return
            tokens = self.headers.get_all("X-Podgrove-Token", [])
            if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.server.token):
                raise WebError("Dashboard authentication required", 403)
            query = parse_qs(target.query, keep_blank_values=True, max_num_fields=4)
            if any(len(values) != 1 for values in query.values()):
                raise WebError("Duplicate request parameters are not allowed", 400)
            stream_match = re.fullmatch(r"/api/environments/([a-f0-9]{12})/logs/stream", target.path)
            if stream_match:
                self._stream(stream_match[1], query)
                return
            if not self.server.request_slots.acquire(blocking=False):
                raise WebError("Dashboard is busy; retry shortly", 429)
            try:
                if target.path == "/api/settings":
                    if not set(query) <= {"namespace"}:
                        raise WebError("Unknown settings parameter", 400)
                    result = self.server.backend.settings(namespace=query.get("namespace", [None])[0])
                elif target.path == "/api/environments" and not query:
                    result = self.server.backend.environments()
                else:
                    match = re.fullmatch(r"/api/environments/([a-f0-9]{12})(/logs)?", target.path)
                    if not match:
                        raise WebError("Unknown endpoint", 404)
                    if match[2]:
                        if not set(query) <= {"source", "service", "tail", "container"}:
                            raise WebError("Unknown log parameter", 400)
                        tail = _parse_log_tail(query.get("tail", ["100"])[0])
                        result = self.server.backend.logs(match[1], source=query.get("source", ["engine"])[0],
                                                          service=query.get("service", [None])[0], tail=tail,
                                                          **({"container": query["container"][0]} if "container" in query else {}))
                    elif query:
                        raise WebError("Details do not accept parameters", 400)
                    else:
                        result = self.server.backend.detail(match[1])
                self._json(200, result)
            finally:
                self.server.request_slots.release()
        except WebError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (ValueError, TypeError, KeyError, AttributeError, PodgroveError):
            self._json(400, {"error": "Invalid dashboard request or environment metadata"})
        except (OSError, http.client.HTTPException):
            try:
                self._json(503, {"error": "Dashboard read is unavailable"})
            except OSError:
                pass

    def _readonly(self):
        self._json(405, {"error": "The dashboard only supports read-only GET requests"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _readonly


def serve(context: str, *, port: int = 0, namespace: str | None = None, open_browser: bool = True) -> int:
    try:
        server = DashboardServer(context, port, namespace=namespace)
    except OSError as exc:
        raise PodgroveError("Cannot bind dashboard to the requested loopback port") from exc
    try:
        print(server.url, flush=True)
        if open_browser:
            try:
                opened = webbrowser.open(server.url)
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                print("podgrove: Browser did not open; use the URL printed above.", file=sys.stderr)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
