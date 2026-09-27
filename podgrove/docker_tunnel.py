"""A Docker API tunnel that preserves TCP half-close through Kubernetes exec.

Containerd's port-forward stream can stop reading Docker output shortly after a
client closes its input. Docker's standard SSH transport uses ``dial-stdio``:
stdin EOF half-closes the daemon socket while stdout remains open until Docker
finishes. Kubernetes exec supplies independent streams with the same semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import selectors
import socket
import struct
import subprocess
import threading
import time

from .errors import PodgroveError
from .kube import ENVIRONMENT, MANAGED, REQUEST_PROCESS_TIMEOUT, Kube, engine_pod_name

MAX_CONNECTIONS = 32
BUFFER_BYTES = 256 * 1024
STDERR_BYTES = 4096
POD_UID_ENV = "PODGROVE_POD_UID"
VERIFY_INTERVAL = 30.0
MAX_VERIFICATION_AGE = 120.0
VERIFY_RETRY_INITIAL = 1.0
VERIFY_RETRY_MAX = 30.0
_UID_REJECTED = "Podgrove engine Pod UID changed; reconnect required"
_UID_GUARD = ('if [ "${PODGROVE_POD_UID:-}" != "$1" ]; then '
              'printf "%s\\n" "Podgrove engine Pod UID changed; reconnect required" >&2; exit 126; '
              'fi; shift; exec "$@"')
_LOG = logging.getLogger(__name__)


class _VerificationUnavailable(PodgroveError):
    """Read-only ownership proof failed without confirming an identity change."""

    def __init__(self, reason):
        super().__init__("Docker engine ownership verification is unavailable")
        self.reason = reason


class EngineReplacedError(PodgroveError):
    """A named engine still exists, but it is not the captured engine."""

    def __init__(self, expected, observed):
        self.expected, self.observed = tuple(expected), tuple(observed)
        super().__init__(
            "Docker engine was replaced: "
            f"StatefulSet UID {expected[0]} -> {observed[0]}; Pod UID {expected[1]} -> {observed[1]}. "
            "Interrupted Docker operations were not replayed; inspect the retained engine and run "
            "podgrove up --refresh to reconnect."
        )


class _StreamFailure(PodgroveError):
    """A single request became uncertain; its bytes must never be replayed."""

    def __init__(self, reason, *, exit_code=None, error_number=None):
        super().__init__(f"Docker API connection failed: {reason}; request was not replayed")
        self.reason, self.exit_code, self.error_number = reason, exit_code, error_number


def _exit_reason(errors):
    # Store classifications, never arbitrary subprocess stderr: it can contain
    # credentials, request arguments, server addresses or unrelated log output.
    message = bytes(errors).lower()
    for needle, reason in ((b"connection reset", "connection_reset"),
                           (b"broken pipe", "broken_pipe"),
                           (b"unexpected eof", "unexpected_eof"),
                           (b"timeout", "timed_out"), (b"timed out", "timed_out"),
                           (b"websocket", "websocket_closed"),
                           (b"failed to exec", "exec_start_failed")):
        if needle in message:
            return reason
    return "transport_exit"


@dataclass
class _Stream:
    client: socket.socket
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    received_bytes: int = 0
    sent_bytes: int = 0
    input_buffer_peak: int = 0
    output_buffer_peak: int = 0


class DockerTunnel:
    def __init__(self, kube, ident: str, port: int, *, max_connections: int = MAX_CONNECTIONS,
                 verification_interval: float = VERIFY_INTERVAL,
                 max_verification_age: float = MAX_VERIFICATION_AGE):
        if max_connections < 1:
            raise ValueError("max_connections must be positive")
        if verification_interval <= 0:
            raise ValueError("verification_interval must be positive")
        if max_verification_age <= 0:
            raise ValueError("max_verification_age must be positive")
        self.kube, self.ident, self.port = kube, ident, port
        self.pod_name = engine_pod_name(ident)
        self.max_connections = max_connections
        self._uids = None
        self._identity_observed = None
        self._identity_state = "unavailable"
        self._identity_checked_at = None
        self._uid_guard = False
        self._verified_at = 0.0
        self._verified_wall = None
        self._verification_interval = verification_interval
        self._max_verification_age = max_verification_age
        self._verification_unavailable = False
        self._verification_pending = False
        self._verification_reason = None
        self._verification_failures = 0
        self._verification_retry_at = None
        self._verification_lock = threading.Lock()
        self._verification_thread = None
        self._listener = None
        self._accept_thread = None
        self._streams: dict[int, _Stream] = {}
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._error = None
        self._failed_connections = 0
        self._last_failure = None
        self._last_failure_log = None

    def snapshot(self):
        """Bounded diagnostics without request contents or remote stderr."""
        with self._lock:
            now = time.monotonic()
            age = max(0.0, now - self._verified_at) if self._verified_wall is not None else None
            state = ("expired" if age is not None and age > self._max_verification_age else
                     "unavailable" if self._verification_unavailable or self._verified_wall is None else
                     "verifying" if self._verification_pending else "verified")
            return {"active_connections": len(self._streams),
                    "failed_connections": self._failed_connections,
                    "last_failure": dict(self._last_failure) if self._last_failure else None,
                    "verification": {"state": state, "last_verified_at": self._verified_wall,
                                     "age_seconds": age, "max_age_seconds": self._max_verification_age,
                                     "consecutive_failures": self._verification_failures,
                                     "retry_in_seconds": (max(0.0, self._verification_retry_at - now)
                                                          if self._verification_retry_at is not None else None),
                                     "reason": self._verification_reason}}

    def identity_snapshot(self):
        """Return captured/current UID evidence without waiting on an API read."""
        def values(uids):
            return {"statefulset_uid": uids[0], "pod_uid": uids[1]} if uids is not None else None
        with self._lock:
            return {"state": self._identity_state, "checked_at": self._identity_checked_at,
                    "namespace": self.kube.namespace, "statefulset": f"pg-{self.ident}", "pod": self.pod_name,
                    "expected": values(self._uids), "observed": values(self._identity_observed)}

    def refresh_identity(self):
        """Make one fresh guarded read pass; never reconnect or replay a request."""
        try:
            self._verify_engine()
        except _VerificationUnavailable as exc:
            self._verification_failed(exc)
            raise
        except Exception as exc:
            self._fail(exc)
            raise
        return self.identity_snapshot()

    def _verification_failed(self, failure):
        with self._lock:
            self._verification_unavailable = True
            self._verification_reason = failure.reason
            self._verification_failures = min(self._verification_failures + 1, 2**31 - 1)
            delay = min(VERIFY_RETRY_MAX, VERIFY_RETRY_INITIAL * 2**min(self._verification_failures - 1, 6))
            self._verification_retry_at = time.monotonic() + delay
            return delay

    def _stream_failed(self, stream, failure):
        if self._stopped.is_set() or self._error is not None:
            return
        now = time.monotonic()
        with self._lock:
            self._failed_connections = min(self._failed_connections + 1, 2**63 - 1)
            self._last_failure = {"reason": failure.reason, "exit_code": failure.exit_code,
                                  "errno": failure.error_number, "at": time.time(),
                                  "received_bytes": stream.received_bytes, "sent_bytes": stream.sent_bytes,
                                  "input_buffer_peak": stream.input_buffer_peak,
                                  "output_buffer_peak": stream.output_buffer_peak}
            report = self._last_failure_log is None or now - self._last_failure_log >= 5
            if report:
                self._last_failure_log = now
        if report:
            _LOG.warning("Docker API connection failed (%s; exit=%s; errno=%s); "
                         "session retained; request was not replayed",
                         failure.reason, failure.exit_code, failure.error_number)

    def _resource(self, kind: str, name: str) -> dict:
        if self._stopped.is_set():
            raise PodgroveError("Docker API tunnel is stopping")
        try:
            result = self.kube.call("get", kind, name, "-o", "json", "--ignore-not-found",
                                    timeout=REQUEST_PROCESS_TIMEOUT, check=False, cancel_event=self._stopped)
        except (PodgroveError, OSError, subprocess.TimeoutExpired) as exc:
            raise _VerificationUnavailable("api_read_failed") from exc
        if result.returncode:
            raise _VerificationUnavailable("api_read_failed")
        try:
            resource = json.loads(result.stdout) if result.stdout.strip() else {}
        except (ValueError, TypeError) as exc:
            raise _VerificationUnavailable("invalid_api_response") from exc
        try:
            metadata = resource.get("metadata", {})
            labels = metadata.get("labels", {})
            if (metadata.get("name") != name or metadata.get("namespace") != self.kube.namespace
                    or not isinstance(metadata.get("uid"), str) or not metadata["uid"] or metadata.get("deletionTimestamp")
                    or labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != self.ident):
                raise ValueError("missing, deleting, or foreign resource")
            return resource
        except (ValueError, TypeError, AttributeError) as exc:
            raise PodgroveError(f"Refusing Docker API access: {kind}/{name} is missing, deleting, or foreign") from exc

    def _verify_engine(self):
        with self._verification_lock:
            self._verify_engine_locked()

    def _verify_engine_locked(self):
        with self._lock:
            self._verification_pending = True
        try:
            self._read_engine_locked()
        except Exception as exc:
            with self._lock:
                if isinstance(exc, EngineReplacedError):
                    self._identity_state = "replaced"
                    self._identity_checked_at = time.time()
                elif self._identity_state != "replaced":
                    # Preserve a confirmed old/new UID pair if a subsequent
                    # diagnostic read is unavailable during teardown.
                    self._identity_state = "unavailable"
                    self._identity_checked_at = time.time()
                    self._identity_observed = None
            raise
        finally:
            with self._lock:
                self._verification_pending = False

    def _read_engine_locked(self):
        controller = self._resource("statefulset", f"pg-{self.ident}")
        pod = self._resource("pod", self.pod_name)
        uids = (controller["metadata"]["uid"], pod["metadata"]["uid"])
        # Compare before controller-reference validation: during replacement
        # the old Pod can still name its old controller, and the changed UID
        # is more useful than a generic owner-reference failure.
        if self._uids is not None and self._uids != uids:
            with self._lock:
                self._identity_observed = uids
                self._identity_state = "replaced"
                self._identity_checked_at = time.time()
            raise EngineReplacedError(self._uids, uids)
        Kube._validate_pod_controller(pod, controller, self.ident)
        claims = [volume["persistentVolumeClaim"].get("claimName")
                  for volume in pod.get("spec", {}).get("volumes", []) if "persistentVolumeClaim" in volume]
        if claims != [f"pg-{self.ident}"]:
            raise PodgroveError("Refusing Docker API access: engine Pod does not use its owned PVC")
        containers = [container for container in pod.get("spec", {}).get("containers", [])
                      if container.get("name") == "docker"]
        fields = [entry for container in containers for entry in container.get("env", [])
                  if entry.get("name") == POD_UID_ENV]
        guarded = (len(containers) == len(fields) == 1 and "value" not in fields[0]
                   and fields[0].get("valueFrom") == {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}})
        if self._uid_guard and not guarded:
            raise PodgroveError("Docker engine Pod lost its verified downward API UID binding")
        self._uids = uids
        self._uid_guard = guarded
        with self._lock:
            if self._identity_state != "replaced":
                self._identity_observed = uids
                self._identity_state = "verified"
                self._identity_checked_at = time.time()
            self._verified_at = time.monotonic()
            self._verified_wall = time.time()
            self._verification_unavailable = False
            self._verification_reason = None
            self._verification_failures = 0
            self._verification_retry_at = None

    def _revalidate(self):
        while not self._stopped.is_set():
            # A connection can discover an outage before this monitor's next
            # regular pass. Observe its retry deadline without waiting another
            # full verification interval, and remain promptly cancellable.
            with self._lock:
                due = (self._verification_retry_at if self._verification_retry_at is not None
                       else self._verified_at + self._verification_interval)
            if self._stopped.wait(min(.2, max(0.0, due - time.monotonic()))):
                return
            if time.monotonic() < due:
                continue
            try:
                self._verify_engine()
            except _VerificationUnavailable as exc:
                if self._stopped.is_set():
                    return
                self._verification_failed(exc)
            except Exception as exc:
                self._fail(exc)
                return

    def start(self):
        self._verify_engine()
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", self.port))
            listener.listen(self.max_connections)
            listener.settimeout(0.2)
            self.port = listener.getsockname()[1]
            self._listener = listener
            self._accept_thread = threading.Thread(target=self._accept, name="podgrove-docker-api", daemon=True)
            self._accept_thread.start()
            # Legacy Pods also need periodic checks: a keepalive connection
            # may outlive ownership changes without opening another stream.
            self._verification_thread = threading.Thread(target=self._revalidate,
                                                         name="podgrove-docker-ownership", daemon=True)
            self._verification_thread.start()
            return self
        except BaseException:
            listener.close()
            raise

    def _fail(self, exc):
        if self._stopped.is_set():
            return
        with self._lock:
            if self._error is not None:
                return
            self._error = str(exc)
            streams = list(self._streams.values())
        # Revocation must also stop already-open HTTP keepalive/build streams.
        # The supervisor may be waiting for a long build and cannot poll check().
        # Close with RST before terminating children: a clean EOF could make a
        # Docker exec client incorrectly report success while its command runs.
        for stream in streams:
            self._reset(stream.client)
            stream.client.close()
            if stream.process is not None and stream.process.poll() is None:
                try:
                    stream.process.terminate()
                except OSError:
                    pass

    @staticmethod
    def _reset(client):
        try:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass

    def _accept(self):
        try:
            while not self._stopped.is_set():
                try:
                    client, _ = self._listener.accept()
                except socket.timeout:
                    continue
                with self._lock:
                    if (self._stopped.is_set() or self._error or self._verification_unavailable
                            or len(self._streams) >= self.max_connections):
                        self._reset(client)
                        client.close()
                        continue
                    stream = _Stream(client)
                    stream.thread = threading.Thread(target=self._serve, args=(stream,),
                                                     name="podgrove-docker-stream", daemon=True)
                    self._streams[id(stream)] = stream
                    stream.thread.start()
        except OSError as exc:
            self._fail(PodgroveError(f"Docker API listener failed: {exc}"))

    def _serve(self, stream):
        try:
            # New Pods prove their immutable identity inside each exec. Legacy
            # Pods still require the full reads for every connection. A stalled
            # background verifier never permits indefinitely stale ownership.
            if self._verification_unavailable:
                raise _StreamFailure("ownership_verification_unavailable")
            if not self._uid_guard or time.monotonic() - self._verified_at > min(self._verification_interval * 2, self._max_verification_age):
                with self._verification_lock:
                    if self._verification_unavailable:
                        raise _StreamFailure("ownership_verification_unavailable")
                    if not self._uid_guard or time.monotonic() - self._verified_at > min(self._verification_interval * 2, self._max_verification_age):
                        self._verify_engine_locked()
            if self._stopped.is_set():
                return
            if self._error:
                raise PodgroveError(self._error)
            remote = ["docker", "--host=unix:///var/run/docker.sock", "system", "dial-stdio"]
            if self._uid_guard:
                remote = ["sh", "-c", _UID_GUARD, "podgrove-docker-guard", self._uids[1], *remote]
            command = self.kube.command(
                "exec", "--request-timeout=0", "-i", self.pod_name, "-c", "docker", "--",
                *remote,
            )
            try:
                stream.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                  stderr=subprocess.PIPE, bufsize=0,
                                                  env={**os.environ, "KUBECTL_REMOTE_COMMAND_WEBSOCKETS": "true"})
                if self._stopped.is_set() or self._error:
                    return  # Failure may have raced with creating this subprocess.
                self._relay(stream)
            except OSError as exc:
                raise _StreamFailure("local_transport_io", error_number=exc.errno) from exc
        except _VerificationUnavailable as exc:
            self._reset(stream.client)
            if not self._stopped.is_set():
                self._verification_failed(exc)
        except _StreamFailure as exc:
            # This connection may have carried a mutating operation. Signal its
            # ambiguous result with RST, keep unrelated streams alive, and never
            # resend even a prefix of the original request to a fresh child.
            self._reset(stream.client)
            self._stream_failed(stream, exc)
        except Exception as exc:
            self._reset(stream.client)
            self._fail(exc)
        finally:
            stream.client.close()
            process = stream.process
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe and not pipe.closed:
                        pipe.close()
            with self._lock:
                self._streams.pop(id(stream), None)

    def _relay(self, stream):
        client, process = stream.client, stream.process
        client.setblocking(False)
        for pipe in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(pipe.fileno(), False)
        incoming, outgoing, errors = bytearray(), bytearray(), bytearray()
        input_closed = output_closed = stderr_closed = False
        guard_rejected = False

        def remember_stderr(chunk):
            nonlocal guard_rejected
            errors.extend(chunk)
            # Keep the fixed revocation marker even if later client diagnostics
            # overflow the bounded stderr suffix, including split marker reads.
            if self._uid_guard and _UID_REJECTED.encode() in errors:
                guard_rejected = True
            del errors[:-STDERR_BYTES]

        with selectors.DefaultSelector() as selector:
            def monitor(target, events, label):
                try:
                    selector.get_key(target)
                except KeyError:
                    if events:
                        selector.register(target, events, label)
                else:
                    if events:
                        selector.modify(target, events, label)
                    else:
                        selector.unregister(target)

            while not self._stopped.is_set() and self._error is None:
                # A read outage does not revoke confirmed ownership, but stale
                # proof cannot authorize an established connection forever.
                # This check is independent of potentially slow API reads.
                if time.monotonic() - self._verified_at > self._max_verification_age:
                    raise _StreamFailure("ownership_verification_expired")
                if input_closed and not incoming and not process.stdin.closed:
                    monitor(process.stdin, 0, "stdin")
                    process.stdin.close()  # EOF only; stdout remains attached.
                monitor(client, (selectors.EVENT_READ if not input_closed and len(incoming) < BUFFER_BYTES else 0)
                        | (selectors.EVENT_WRITE if outgoing else 0), "client")
                if not process.stdin.closed:
                    monitor(process.stdin, selectors.EVENT_WRITE if incoming else 0, "stdin")
                if not output_closed:
                    monitor(process.stdout, selectors.EVENT_READ if len(outgoing) < BUFFER_BYTES else 0, "stdout")
                if not stderr_closed:
                    monitor(process.stderr, selectors.EVENT_READ, "stderr")
                exit_code = process.poll()
                if exit_code is not None and exit_code != 0:
                    # A clean EOF here could make Docker report exit 0 for a still
                    # running exec. Reset the client stream on transport failure.
                    if not stderr_closed:
                        while True:
                            try:
                                chunk = os.read(process.stderr.fileno(), 65536)
                            except BlockingIOError:
                                break
                            if not chunk:
                                break
                            remember_stderr(chunk)
                    if guard_rejected:
                        raise PodgroveError(_UID_REJECTED)
                    raise _StreamFailure(_exit_reason(errors), exit_code=exit_code)
                if exit_code == 0 and output_closed and not outgoing:
                    try:
                        client.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass  # The Docker client may already have disconnected.
                    return
                for key, events in selector.select(timeout=0.2):
                    try:
                        if key.data == "client":
                            try:
                                if events & selectors.EVENT_READ:
                                    chunk = client.recv(min(65536, BUFFER_BYTES - len(incoming)))
                                    if chunk:
                                        incoming.extend(chunk)
                                        stream.received_bytes += len(chunk)
                                        stream.input_buffer_peak = max(stream.input_buffer_peak, len(incoming))
                                    else:
                                        input_closed = True
                                if events & selectors.EVENT_WRITE:
                                    sent = client.send(outgoing)
                                    del outgoing[:sent]
                                    stream.sent_bytes += sent
                            except (ConnectionResetError, BrokenPipeError):
                                return  # Client cancellation is local to this stream.
                        elif key.data == "stdin":
                            try:
                                del incoming[:os.write(process.stdin.fileno(), incoming)]
                            except BrokenPipeError:
                                # The daemon can reject a request before consuming
                                # its body. Still deliver its response to the client.
                                incoming.clear()
                                input_closed = True
                        elif key.data == "stdout":
                            chunk = os.read(process.stdout.fileno(), min(65536, BUFFER_BYTES - len(outgoing)))
                            if chunk:
                                outgoing.extend(chunk)
                                stream.output_buffer_peak = max(stream.output_buffer_peak, len(outgoing))
                            else:
                                monitor(process.stdout, 0, "stdout")
                                output_closed = True
                        elif key.data == "stderr":
                            chunk = os.read(process.stderr.fileno(), 65536)
                            if chunk:
                                remember_stderr(chunk)
                            else:
                                monitor(process.stderr, 0, "stderr")
                                stderr_closed = True
                    except BlockingIOError:
                        continue

    def check(self):
        if self._error:
            raise PodgroveError(self._error)
        if self._accept_thread is None or not self._accept_thread.is_alive():
            raise PodgroveError("Docker API tunnel is disconnected; run podgrove up to reconnect")

    def close(self):
        self._stopped.set()
        if self._listener:
            self._listener.close()
        with self._lock:
            streams = list(self._streams.values())
        for stream in streams:
            try:
                stream.client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if stream.process is not None and stream.process.poll() is None:
                stream.process.terminate()
        deadline = time.monotonic() + 7
        for thread in [self._accept_thread, self._verification_thread, *(stream.thread for stream in streams)]:
            if thread:
                thread.join(timeout=max(0, deadline - time.monotonic()))
