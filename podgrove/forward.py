"""Loopback-only port allocation and supervised kubectl tunnels."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time

from .errors import PodgroveError
from .kube import ENVIRONMENT, MANAGED, REQUEST_PROCESS_TIMEOUT, Kube, engine_pod_name


def free_port(preferred: int = 0) -> int:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError as exc:
            raise PodgroveError(f"Local port {preferred} is already in use; refusing to attach to another worktree") from exc
        return sock.getsockname()[1]


def _selected_ports(published: list[dict], configured: list[dict] | None) -> list[dict]:
    chosen = [dict(port) for port in published] if configured is None else []
    if configured is not None:
        for rule in configured:
            matches = [p for p in published if p["service"] == rule["service"] and p["target"] == rule["port"]]
            if len(matches) != 1:
                raise PodgroveError(f"forward: {rule['service']}:{rule['port']} must identify exactly one published TCP port")
            chosen.append({**matches[0], "local": rule.get("local")})
    seen = set()
    for port in chosen:
        key = (port["service"], port["target"])
        if key in seen:
            raise PodgroveError(f"forward: {key[0]}:{key[1]} must identify exactly one published TCP port")
        seen.add(key)
        if port.get("protocol", "tcp") != "tcp":
            raise PodgroveError(f"services.{port['service']}.ports: UDP forwarding is unsupported")
        _declared_bounds(port)
    return chosen


def _declared_bounds(port: dict) -> tuple[int, int]:
    declared = port.get("declared_published", port["published"])
    try:
        if type(declared) is int:
            bounds = (1, 65535) if declared == 0 else (declared, declared)
        elif isinstance(declared, str) and "-" in declared:
            bounds = tuple(int(part) for part in declared.split("-"))
        else:
            raise ValueError
        if len(bounds) != 2 or not 1 <= bounds[0] <= bounds[1] <= 65535:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise PodgroveError(f"services.{port['service']}.ports: invalid published port or range") from exc
    if declared != 0 and bounds[0] <= 2375 <= bounds[1]:
        raise PodgroveError(f"services.{port['service']}.ports: 2375 is reserved for the Docker API")
    return bounds


def _dynamic(port: dict) -> bool:
    declared = port.get("declared_published", port["published"])
    return declared == 0 or isinstance(declared, str)


def _local_ports(chosen: list[dict], ident: str) -> list[int]:
    offset = int(ident[:8], 16) % 20000
    used = set()
    result = []
    for port in chosen:
        local = port.get("local")
        if local is not None:
            local = free_port(int(local))
            if local in used:
                raise PodgroveError(f"forward.local: duplicate port {local}")
        else:
            # Deterministic preference, then a free port. Never reuse a listener.
            # Docker may choose a new host port after recreation. Base dynamic
            # local preferences on the stable container target instead.
            preferred = port["target"] if _dynamic(port) else port["published"]
            local = 20000 + (preferred + offset) % 40000
            for _ in range(40000):
                if local not in used:
                    try:
                        free_port(local)
                        break
                    except PodgroveError:
                        pass
                local = 20000 + (local - 20000 + 1) % 40000
            else:
                raise PodgroveError("No free local application port")
        used.add(local)
        result.append(local)
    return result


def validate_port_plan(published: list[dict], configured: list[dict] | None, ident: str) -> None:
    """Check selection and local availability before starting the remote stack.

    Dynamic Docker ports do not exist yet. This never invents a remote port or
    returns a forwarding plan that a caller could mistake for a ready endpoint.
    """
    _local_ports(_selected_ports(published, configured), ident)


class PortMappingError(PodgroveError):
    """An observed Compose publisher cannot safely back a local endpoint."""


def _observed_port(port: dict, rows: list[dict], project: str | None) -> int:
    label = f"{port['service']}:{port['target']}"
    guidance = "Refresh with podgrove up --refresh to resolve current published ports."
    matches = [row for row in rows if isinstance(row, dict) and row.get("Service") == port["service"]]
    if len(matches) != 1 or matches[0].get("State") != "running":
        raise PortMappingError(f"{label}: forwarding requires exactly one running service container. {guidance}")
    row = matches[0]
    if project is not None and row.get("Project") != project:
        raise PortMappingError(f"{label}: observed container belongs to a different Compose project; refusing forwarding")
    publishers = row.get("Publishers")
    if not isinstance(publishers, list):
        raise PortMappingError(f"{label}: no published TCP port was observed. {guidance}")
    ports = set()
    for binding in publishers:
        if not isinstance(binding, dict):
            raise PortMappingError(f"{label}: malformed published port observation")
        if binding.get("TargetPort") != port["target"] or binding.get("Protocol") != "tcp":
            continue
        remote = binding.get("PublishedPort")
        if (type(binding.get("TargetPort")) is not int or type(remote) is not int
                or not 1 <= remote <= 65535 or remote == 2375
                or binding.get("URL") not in ("0.0.0.0", "127.0.0.1", "::", "::1", "")):
            raise PortMappingError(f"{label}: unsafe or malformed published TCP port observation")
        lower, upper = _declared_bounds(port)
        if not lower <= remote <= upper:
            raise PortMappingError(f"{label}: observed port does not match its Compose declaration. {guidance}")
        ports.add(remote)
    if len(ports) != 1:
        raise PortMappingError(f"{label}: expected one unambiguous published TCP port. {guidance}")
    return ports.pop()


def port_plan(published: list[dict], configured: list[dict] | None, ident: str, *,
              observed: list[dict] | None = None, project: str | None = None) -> list[dict]:
    chosen = _selected_ports(published, configured)
    resolved = []
    for port in chosen:
        if observed is not None:
            remote = _observed_port(port, observed, project)
        elif _dynamic(port):
            raise PortMappingError(f"{port['service']}:{port['target']}: dynamic forwarding requires observed Docker publishers")
        else:
            remote = port["published"]
        metadata = {"declared_published": port.get("declared_published", port["published"])} if _dynamic(port) else {}
        resolved.append({**port, **metadata, "published": remote})
    return [{**port, "local": local, "url": f"http://127.0.0.1:{local}"}
            for port, local in zip(resolved, _local_ports(resolved, ident))]


def verify_port_mappings(ports: list[dict], rows: list[dict], project: str | None) -> None:
    """Detect recreation or missing mappings without reallocating local ports."""
    for port in ports:
        if _observed_port(port, rows, project) != port["published"]:
            raise PortMappingError(
                f"{port['service']}:{port['target']}: Docker reassigned its published port; "
                "forwarding stopped. Run podgrove up --refresh to reconnect."
            )


class ForwardOwnershipError(PodgroveError):
    """The captured engine is no longer safe to forward to."""


class Tunnel:
    """Restart application forwards without changing their ports or engine UID.

    A dedicated monitor checks even when there are no source edits, and while
    the main supervisor is busy with health/heartbeat requests. Retry exhaustion
    leaves the session alive but its endpoints explicitly disconnected.
    """

    def __init__(self, kube, ident: str, ports: list[tuple[int, int]], *,
                 poll_interval=0.5, retry_delays=(0.25, 1.0, 2.0), verification_interval=30.0):
        self.kube, self.ident, self.ports = kube, ident, ports
        self.process = None
        self.log = None
        self.on_change = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._thread = None
        self._uids = None
        self._verified_at = 0.0
        self._poll_interval = poll_interval
        self._retry_delays = tuple(retry_delays)
        self._verification_interval = verification_interval
        self._ready_at = 0.0
        self._timeout = 30
        self._status = {"state": "disconnected", "error": None, "attempts": 0,
                        "changed_at": time.time(), "checked_at": time.time()}

    def snapshot(self):
        with self._lock:
            return dict(self._status)

    def _report(self, state, error=None, attempts=None):
        with self._lock:
            previous = dict(self._status)
            self._status.update(state=state, error=error, checked_at=time.time())
            if attempts is not None:
                self._status["attempts"] = attempts
            changed = any(self._status[key] != previous[key] for key in ("state", "error", "attempts"))
            if changed:
                self._status["changed_at"] = self._status["checked_at"]
            current = dict(self._status)
        if changed and self.on_change:
            self.on_change(current)

    def _verify_engine(self):
        found = []
        for kind, name in (("statefulset", "pg-" + self.ident), ("pod", engine_pod_name(self.ident))):
            if self._stopped.is_set():
                raise PodgroveError("Application forwarding cancelled")
            response = self.kube.call("get", kind, name, "-o", "json", "--ignore-not-found",
                                      timeout=REQUEST_PROCESS_TIMEOUT)
            try:
                resource = json.loads(response.stdout) if response.stdout.strip() else {}
                meta = resource.get("metadata", {})
                labels = meta.get("labels", {})
                if (meta.get("name") != name or meta.get("namespace") != self.kube.namespace
                        or not meta.get("uid") or meta.get("deletionTimestamp")
                        or labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != self.ident):
                    raise ValueError("missing, terminating or foreign engine")
            except (ValueError, TypeError, AttributeError) as exc:
                raise ForwardOwnershipError(f"Application forwarding refused: {kind}/{name} ownership changed") from exc
            found.append(resource)
        try:
            Kube._validate_pod_controller(found[1], found[0], self.ident)
        except PodgroveError as exc:
            raise ForwardOwnershipError(f"Application forwarding refused: {exc}") from exc
        uids = tuple(resource["metadata"]["uid"] for resource in found)
        if self._uids is not None and self._uids != uids:
            raise ForwardOwnershipError("Application engine Pod or StatefulSet was replaced; run podgrove up to reconnect")
        self._uids = uids
        self._verified_at = time.monotonic()

    def _output(self):
        if self.log is None:
            return ""
        size = os.fstat(self.log.fileno()).st_size
        # pread does not move the file offset inherited by kubectl's writers.
        return os.pread(self.log.fileno(), min(size, 65536), max(0, size - 65536)).decode(errors="replace")

    def _listeners_ready(self):
        for local, _ in self.ports:
            try:
                with socket.create_connection(("127.0.0.1", local), timeout=0.15):
                    pass
            except OSError:
                return False
        return True

    def _launch(self):
        self._verify_engine()
        with self._process_lock:
            if self._stopped.is_set():
                raise PodgroveError("Application forwarding cancelled")
            self.log = tempfile.TemporaryFile(mode="w+b")
            try:
                self.process = subprocess.Popen(self.kube.command(
                    "port-forward", f"pod/{engine_pod_name(self.ident)}", "--address=127.0.0.1", "--request-timeout=0",
                    *[f"{local}:{remote}" for local, remote in self.ports]),
                    stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
            except BaseException:
                self.log.close()
                self.log = None
                raise
        deadline = time.monotonic() + self._timeout
        while not self._stopped.is_set() and time.monotonic() < deadline:
            output = self._output()
            if self.process.poll() is not None:
                raise PodgroveError(f"Port-forward failed: {output[-2000:]}")
            if (all(f"Forwarding from 127.0.0.1:{local} ->" in output for local, _ in self.ports)
                    and self._listeners_ready()):
                # The named Pod must still be the captured object after kubectl
                # resolves it. A replacement must never inherit these listeners.
                self._verify_engine()
                if self.process.poll() is not None or not self._listeners_ready():
                    raise PodgroveError("Port-forward stopped while verifying engine ownership")
                self._ready_at = time.monotonic()
                return
            self._stopped.wait(0.05)
        raise PodgroveError("Application forwarding cancelled" if self._stopped.is_set()
                            else "Port-forward did not become ready in time")

    def _dispose(self):
        with self._process_lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
            if self.log:
                self.log.close()
                self.log = None

    def start(self, timeout=30):
        self._timeout = timeout
        try:
            self._launch()
            self._report("ready")
            self._thread = threading.Thread(target=self._monitor, name="podgrove-app-forward", daemon=True)
            self._thread.start()
            return self
        except BaseException:
            self._dispose()
            raise

    def _monitor(self):
        attempts = 0
        try:
            while not self._stopped.wait(self._poll_interval):
                try:
                    if self.process.poll() is not None:
                        raise PodgroveError("Kubernetes application port-forward exited")
                    if not self._listeners_ready():
                        raise PodgroveError("Kubernetes application port-forward listener is unavailable")
                    if time.monotonic() - self._verified_at >= self._verification_interval:
                        self._verify_engine()
                    if time.monotonic() - self._ready_at >= 30:
                        attempts = 0
                    self._report("ready", attempts=attempts)
                    continue
                except ForwardOwnershipError:
                    raise
                except (PodgroveError, OSError) as failure:
                    self._report("reconnecting", str(failure), attempts)
                self._dispose()
                while not self._stopped.is_set():
                    if attempts >= len(self._retry_delays):
                        last = self.snapshot()["error"]
                        self._report("disconnected", "Automatic forwarding retries exhausted; run podgrove up to reconnect"
                                     + (f". Last failure: {last}" if last else ""), attempts)
                        return
                    delay = self._retry_delays[attempts]
                    attempts += 1
                    self._report("reconnecting", self.snapshot()["error"], attempts)
                    if self._stopped.wait(delay):
                        return
                    try:
                        self._launch()
                        self._report("ready", attempts=attempts)
                        break
                    except ForwardOwnershipError:
                        raise
                    except (PodgroveError, OSError) as failure:
                        self._report("reconnecting", str(failure), attempts)
                        self._dispose()
        except Exception as failure:
            self._report("disconnected", str(failure), attempts)
        finally:
            if self.snapshot()["state"] != "ready" or self._stopped.is_set():
                self._dispose()

    def check(self):
        # Forward failures are represented explicitly instead of tearing down
        # the Docker transport, synchronizer and unrelated control operations.
        return self.snapshot()

    def close(self):
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=8)
            if self._thread.is_alive():
                raise PodgroveError("Application forwarding monitor did not stop")
        self._dispose()
        self._report("disconnected", "Application forwarding stopped")
