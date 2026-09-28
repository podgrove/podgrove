"""Managed reverse TCP listeners over the original engine's authenticated exec."""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

from . import reverse_protocol
from .docker_tunnel import _UID_GUARD
from .errors import PodgroveError
from .exec_transport import _stop
from .kube import engine_pod_name
from .sync_recovery import SyncOwnershipError, SyncRecoveryUnavailable, verify_engine

HELPER_IMAGE = "python:3.12-alpine"
IDENTITY_LABEL = "io.podgrove.reverse.identity"
OWNER_LABEL = "io.podgrove.reverse.owner"
RETRY_DELAYS = (.25, 1.0, 2.0)
VERIFY_INTERVAL = 15.0
MAX_VERIFICATION_AGE = 120.0
INSPECT_FORMAT = '{"Id":{{json .Id}},"Name":{{json .Name}},"Labels":{{json .Config.Labels}}}'
BRIDGE_FORMAT = '{"Name":{{json .Name}},"Driver":{{json .Driver}},"IPAM":{"Config":{{json .IPAM.Config}}}}'


class ReverseUnavailable(PodgroveError):
    """A fresh managed channel may be attempted without replaying application bytes."""

    def __init__(self, message, *, reason="operation_unavailable", returncode=None):
        super().__init__(message)
        self.reason, self.returncode = reason, returncode


def normalize_mappings(mappings):
    """Accept only explicit local loopback targets and unprivileged remote ports."""
    if not isinstance(mappings, list) or not 1 <= len(mappings) <= 32:
        raise PodgroveError("reverse: expected between one and 32 port mappings")
    result = []
    ports = set()
    for mapping in mappings:
        if not isinstance(mapping, dict) or set(mapping) - {"local_port", "remote_port", "local_host"}:
            raise PodgroveError("reverse: expected local_port, optional remote_port and local_host")
        local = mapping.get("local_port")
        remote = mapping.get("remote_port", local)
        host = mapping.get("local_host", "127.0.0.1")
        if type(local) is not int or not 1 <= local <= 65535:
            raise PodgroveError("reverse.local_port: expected a TCP port between 1 and 65535")
        if type(remote) is not int or not 1024 <= remote <= 65535 or remote in (2375, 2376):
            raise PodgroveError("reverse.remote_port: use 1024–65535, excluding Docker ports 2375 and 2376")
        if host not in ("127.0.0.1", "::1"):
            raise PodgroveError("reverse.local_host: only literal 127.0.0.1 or ::1 is allowed")
        if remote in ports:
            raise PodgroveError("reverse.remote_port: duplicate remote listener")
        ports.add(remote)
        result.append({"local_host": host, "local_port": local, "remote_port": remote})
    return result


class ReverseForward:
    """Own one restricted helper and reconnect listeners without replaying TCP data."""

    def __init__(self, kube, ident, mappings, expected_uids, *, image=HELPER_IMAGE,
                 startup_timeout=180.0, verification_interval=VERIFY_INTERVAL,
                 max_verification_age=MAX_VERIFICATION_AGE):
        self.pod_name = engine_pod_name(ident)
        if (not isinstance(expected_uids, dict)
                or any(not isinstance(expected_uids.get(key), str) or not expected_uids[key]
                       for key in ("statefulset_uid", "pod_uid"))):
            raise PodgroveError("Reverse forwarding requires the captured engine identity")
        if not isinstance(image, str) or not image or image.startswith("-") or any(ch.isspace() for ch in image):
            raise PodgroveError("Reverse helper image is invalid")
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in (startup_timeout, verification_interval, max_verification_age)):
            raise ValueError("Reverse lifecycle budgets must be positive")
        self.kube, self.ident = kube, ident
        self.mappings = normalize_mappings(mappings)
        self.expected = {key: expected_uids[key] for key in ("statefulset_uid", "pod_uid")}
        self.image = image
        self.startup_timeout = startup_timeout
        self.verification_interval = verification_interval
        self.max_verification_age = max_verification_age
        self._stop = threading.Event()
        self._startup = threading.Event()
        self._generation = threading.Event()
        self._lock = threading.Lock()
        self._guard_lock = threading.Lock()
        self._worker = self._monitor = None
        self._process = self._peer = None
        self._name = self._nonce = self._container_id = None
        self._creation_uncertain = False
        self._state = "stopped"
        self._error = self._fatal = None
        self._attempts = 0
        self._verified_at = 0.0
        self._verification = "unavailable"
        self._ready_at = 0.0
        self._cleanup_error = None
        self._last_stats = {}
        self._stage = "idle"
        self._first_failure = self._last_failure = self._cleanup_failure = None

    def _set(self, state, error=None):
        with self._lock:
            self._state, self._error = state, error

    def snapshot(self):
        with self._lock:
            stats = self._peer.snapshot() if self._peer is not None else dict(self._last_stats)
            state = "degraded" if self._state == "ready" and self._verification != "verified" else self._state
            return {"state": state, "error": self._error, "attempts": self._attempts,
                    "helper_id": self._container_id, "cleanup_error": self._cleanup_error,
                    "helper_name": self._name,
                    "stage": self._stage, "first_failure": self._first_failure,
                    "last_failure": self._last_failure, "cleanup_failure": self._cleanup_failure,
                    "verification": self._verification, "mappings": [dict(item) for item in self.mappings],
                    **stats}

    def check(self):
        if self._fatal is not None:
            raise PodgroveError(self._fatal)

    def _diagnostic(self, error):
        reason = "operation_unavailable"
        if isinstance(error, ReverseUnavailable):
            reason = error.reason
        elif isinstance(error, SyncOwnershipError):
            reason = "ownership_changed"
        elif isinstance(error, SyncRecoveryUnavailable):
            reason = "ownership_unavailable"
        elif isinstance(error, reverse_protocol.ProtocolError):
            reason = "protocol_error"
        elif isinstance(error, OSError):
            reason = "os_error"
        value = {"stage": self._stage, "reason": reason, "attempt": self._attempts}
        code = getattr(error, "returncode", None)
        if type(code) is int:
            value["returncode"] = code
        return value

    def _record_failure(self, error):
        value = self._diagnostic(error)
        self._last_failure = value
        if self._first_failure is None:
            self._first_failure = value
        return value

    def _guard(self, cancel=None, timeout=10):
        cancel = self._stop if cancel is None else cancel
        while not self._guard_lock.acquire(timeout=.1):
            if cancel.is_set():
                raise ReverseUnavailable("Reverse ownership verification cancelled")
        try:
            verify_engine(self.kube, self.ident, self.expected, cancel, timeout=timeout)
            with self._lock:
                self._verified_at = time.monotonic()
                self._verification = "verified"
        finally:
            self._guard_lock.release()

    def _exec_arguments(self, *arguments):
        return ["exec", "--request-timeout=0", "-i", self.pod_name, "-c", "docker", "--",
                "sh", "-c", _UID_GUARD, "podgrove-reverse-guard", self.expected["pod_uid"],
                "docker", "--host=unix:///var/run/docker.sock", *arguments]

    def _command(self, *arguments):
        return self.kube.command(*self._exec_arguments(*arguments))

    def _docker(self, *arguments, cancel=None, timeout=10):
        cancel = self._stop if cancel is None else cancel
        try:
            return self.kube.call(*self._exec_arguments(*arguments), timeout=timeout, check=False,
                                  cancel_event=cancel,
                                  env={**os.environ, "KUBECTL_REMOTE_COMMAND_WEBSOCKETS": "true"})
        except (PodgroveError, OSError, subprocess.TimeoutExpired) as exc:
            if cancel.is_set():
                reason = "cancelled"
            elif isinstance(exc, subprocess.TimeoutExpired) or " timed out after " in str(exc):
                reason = "timeout"
            elif isinstance(exc, PermissionError):
                reason = "permission_denied"
            elif isinstance(exc, FileNotFoundError):
                reason = "executable_missing"
            elif isinstance(exc, OSError):
                reason = "os_error"
            else:
                reason = "command_unavailable"
            raise ReverseUnavailable("Reverse helper operation is unavailable", reason=reason) from exc

    def _inspect(self, reference, *, cancel=None, timeout=10, stage="inspect_helper"):
        self._stage = stage
        result = self._docker("container", "inspect", "--format", INSPECT_FORMAT, reference, cancel=cancel, timeout=timeout)
        if result.returncode:
            if "No such" in result.stderr:
                return None
            raise ReverseUnavailable("Reverse helper identity could not be read", reason="command_exit", returncode=result.returncode)
        try:
            value = json.loads(result.stdout)
            ident = value["Id"]
            labels = value["Labels"]
            if (not isinstance(ident, str) or re.fullmatch(r"[a-f0-9]{64}", ident) is None
                    or labels.get(IDENTITY_LABEL) != self.ident or labels.get(OWNER_LABEL) != self._nonce
                    or value["Name"] != f"/{self._name}"
                    or (self._container_id is not None and ident != self._container_id)):
                raise ValueError
            return ident
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise SyncOwnershipError("Reverse helper ownership changed; refusing cleanup or adoption") from exc

    def _gateway(self):
        self._stage = "inspect_bridge"
        result = self._docker("network", "inspect", "--format", BRIDGE_FORMAT, "bridge")
        try:
            network = json.loads(result.stdout)
            candidates = []
            for item in network["IPAM"]["Config"]:
                subnet = ipaddress.ip_network(item["Subnet"])
                if subnet.version != 4:
                    continue
                address = ipaddress.ip_address(item.get("Gateway", ""))
                if address.version == 4 and address in subnet:
                    candidates.append(address)
            if (result.returncode or network["Name"] != "bridge"
                    or network["Driver"] != "bridge" or len(candidates) != 1
                    or not candidates[0].is_private or candidates[0].is_loopback
                    or candidates[0].is_link_local or candidates[0].is_unspecified or candidates[0].is_multicast):
                raise ValueError
            return str(candidates[0])
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise ReverseUnavailable("Reverse forwarding requires the engine's private default-bridge gateway",
                                     reason="command_exit" if result.returncode else "invalid_bridge_metadata",
                                     returncode=result.returncode if result.returncode else None) from exc

    def _create(self, gateway):
        self._stage = "create_helper"
        self._nonce = secrets.token_hex(16)
        self._name = f"podgrove-reverse-{self.ident}-{self._nonce}"
        self._creation_uncertain = True
        config = json.dumps({"nonce": self._nonce, "bind": gateway,
                             "ports": [item["remote_port"] for item in self.mappings]}, separators=(",", ":"))
        program = Path(reverse_protocol.__file__).read_text(encoding="utf-8")
        result = self._docker("create", "--interactive", "--rm", "--name", self._name,
                              "--label", f"{IDENTITY_LABEL}={self.ident}", "--label", f"{OWNER_LABEL}={self._nonce}",
                              "--network", "host", "--read-only", "--user", "65534:65534", "--cap-drop", "ALL",
                              "--security-opt", "no-new-privileges", "--pids-limit", "32",
                              self.image, "python3", "-I", "-S", "-B", "-u", "-c", program, config,
                              timeout=min(self.startup_timeout, 120))
        ident = result.stdout.strip()
        if result.returncode or re.fullmatch(r"[a-f0-9]{64}", ident) is None:
            raise ReverseUnavailable("Reverse helper creation failed; no application requests were replayed",
                                     reason="command_exit" if result.returncode else "invalid_create_response",
                                     returncode=result.returncode if result.returncode else None)
        self._container_id = ident
        self._creation_uncertain = False
        if self._inspect(ident) != ident:
            raise ReverseUnavailable("Reverse helper disappeared before startup")

    def _cleanup(self, *, final=False):
        if self._name is None:
            return
        cancel = threading.Event() if final else self._stop
        deadline = time.monotonic() + 5
        self._stage = "cleanup_verify_engine"
        self._guard(cancel, timeout=max(.1, deadline - time.monotonic()))
        ident = self._inspect(self._container_id or self._name, cancel=cancel,
                              timeout=max(.1, deadline - time.monotonic()), stage="cleanup_inspect_helper")
        if ident is None and self._creation_uncertain:
            raise ReverseUnavailable("Reverse helper creation outcome is still uncertain; refusing another helper")
        if ident is not None:
            self._container_id = ident
            self._stage = "cleanup_remove_helper"
            result = self._docker("rm", "--force", ident, cancel=cancel,
                                  timeout=max(.1, deadline - time.monotonic()))
            if result.returncode and "No such" not in result.stderr:
                raise ReverseUnavailable("Reverse helper cleanup could not be verified", reason="command_exit", returncode=result.returncode)
            if self._inspect(ident, cancel=cancel, timeout=max(.1, deadline - time.monotonic()),
                             stage="cleanup_confirm_absence") is not None:
                raise ReverseUnavailable("Reverse helper still exists after cleanup; refusing another helper")
        self._container_id = self._name = self._nonce = None
        self._creation_uncertain = False
        self._cleanup_error = None
        self._cleanup_failure = None

    def _ready(self):
        if self._stop.is_set() or self._generation.is_set():
            raise ReverseUnavailable("Reverse startup was cancelled before listener readiness")
        self._ready_at = time.monotonic()
        self._stage = "streaming"
        self._set("ready")
        self._startup.set()

    def _ownership_monitor(self):
        while not self._stop.wait(self.verification_interval):
            try:
                self._guard()
            except SyncOwnershipError:
                self._fatal = "Reverse engine ownership changed; reconnect explicitly after inspecting the environment"
                self._verification = "replaced"
                self._generation.set()
                return
            except (SyncRecoveryUnavailable, ReverseUnavailable, PodgroveError):
                self._verification = "unavailable"
                if time.monotonic() - self._verified_at > self.max_verification_age:
                    self._generation.set()

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    if self._fatal:
                        break
                    self._generation = threading.Event()
                    self._stage = "verify_engine"
                    self._guard()
                    self._cleanup()
                    self._create(self._gateway())
                    self._stage = "verify_created_engine"
                    self._guard()
                    if self._stop.is_set():
                        break
                    self._stage = "attach_helper"
                    process = subprocess.Popen(self._command("start", "--attach", "--interactive", self._container_id),
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               bufsize=0, start_new_session=True,
                                               env={**os.environ, "KUBECTL_REMOTE_COMMAND_WEBSOCKETS": "true"})
                    self._process = process
                    targets = {item["remote_port"]: (item["local_host"], item["local_port"]) for item in self.mappings}
                    peer = reverse_protocol.Peer(process.stdout.fileno(), process.stdin.fileno(),
                                                 nonce=self._nonce, ports=list(targets), targets=targets,
                                                 cancel=self._generation, ready=self._ready,
                                                 allow_open=lambda: self._verification == "verified",
                                                 check_active=lambda: time.monotonic() - self._verified_at <= self.max_verification_age,
                                                 stderr=process.stderr.fileno())
                    self._peer = peer
                    if self._stop.is_set():
                        self._generation.set()
                    self._stage = "wait_ready"
                    peer.run()
                    if not self._stop.is_set():
                        raise ReverseUnavailable("Reverse channel lost its verified ownership budget")
                except SyncOwnershipError as error:
                    self._record_failure(error)
                    self._fatal = "Reverse engine or helper ownership changed; explicit reconnect is required"
                except (reverse_protocol.ProtocolError, SyncRecoveryUnavailable, ReverseUnavailable, OSError, PodgroveError) as error:
                    if not self._stop.is_set():
                        detail = self._record_failure(error)
                        self._set("reconnecting", f"Reverse {detail['stage']} failed ({detail['reason']}); existing TCP requests were not replayed")
                finally:
                    if self._peer is not None:
                        self._last_stats = self._peer.snapshot()
                        self._peer = None
                    if self._process is not None:
                        process, self._process = self._process, None
                        process.stdin.close()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            pass
                        _stop(process)
                        for pipe in (process.stdin, process.stdout, process.stderr):
                            if pipe is not None:
                                pipe.close()
                if self._stop.is_set() or self._fatal:
                    break
                if self._ready_at and time.monotonic() - self._ready_at >= 30:
                    self._attempts = 0
                self._ready_at = 0.0
                if self._attempts >= len(RETRY_DELAYS):
                    break
                delay = RETRY_DELAYS[self._attempts]
                self._attempts += 1
                if self._stop.wait(delay):
                    break
        finally:
            try:
                self._cleanup(final=True)
            except (SyncOwnershipError, SyncRecoveryUnavailable, ReverseUnavailable, PodgroveError) as error:
                self._cleanup_failure = self._diagnostic(error)
                self._cleanup_error = "Reverse helper cleanup could not be verified; inspect the retained owned helper"
            if self._stop.is_set():
                self._set("stopped")
            else:
                cause = self._first_failure
                detail = f" during {cause['stage']} ({cause['reason']})" if cause else ""
                self._set("disconnected", self._fatal or f"Reverse reconnect attempts exhausted{detail}; run podgrove up --refresh")
            self._startup.set()

    def start(self):
        if self._worker is not None or self._stop.is_set():
            raise PodgroveError("Reverse forwarding has already been started or closed")
        self._set("starting")
        self._worker = threading.Thread(target=self._run, name="podgrove-reverse", daemon=True)
        self._worker.start()
        self._monitor = threading.Thread(target=self._ownership_monitor, name="podgrove-reverse-ownership", daemon=True)
        self._monitor.start()
        if not self._startup.wait(self.startup_timeout) or self._state != "ready" or self._stop.is_set():
            error = ("Reverse forwarding startup was cancelled" if self._stop.is_set() else
                     self._error or "Reverse listeners did not become ready before their startup deadline")
            self.close()
            raise PodgroveError(error)
        return self

    def cancel(self):
        """Wake startup and interrupt owned operations without waiting for cleanup."""
        self._stop.set()
        self._generation.set()
        self._startup.set()

    def close(self):
        self.cancel()
        for thread in (self._worker, self._monitor):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=18)
                if thread.is_alive():
                    raise PodgroveError("Reverse forwarding cleanup did not finish within its deadline")
