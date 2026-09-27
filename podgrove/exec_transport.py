"""Stream exec through one owned Kubernetes exec, without nested HTTP hijacking.

An interrupted command is never replayed: it may already have changed state.
Non-TTY output is streamed through bounded checksum and completion relays. The
engine waits for a separate acknowledgment before closing its remote streams.
Native TTY execution keeps terminal resize/input semantics and does not promise
byte-for-byte checksums for terminal-transformed output.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import select
import secrets
import selectors
import signal
import subprocess
import sys
import threading
import time
import uuid

from .docker_tunnel import POD_UID_ENV, _UID_GUARD
from .errors import PodgroveError
from .kube import ENVIRONMENT, MANAGED, REQUEST_PROCESS_TIMEOUT, Kube, engine_pod_name

VERIFY_INTERVAL = 5.0
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
NUMBER_LABEL = "com.docker.compose.container-number"
ONEOFF_LABEL = "com.docker.compose.oneoff"
EXIT_DRAIN_TIMEOUT = 10.0
OUTPUT_BUFFER_BYTES = 256 * 1024
# Every argument remains a separate argv item. These shell programs only use an
# internally generated nonce; they never evaluate the application command text.
_STREAM_WRAPPER = r'''
nonce=$1
owner=$2
shift 2
case "$nonce$owner" in *[!0-9a-f]*|'') exit 125;; esac
[ "${#nonce}" -eq 32 ] && [ "${#owner}" -eq 32 ] || exit 125
for utility in mkfifo tee sha256sum; do command -v "$utility" >/dev/null 2>&1 || exit 125; done
d=/tmp/podgrove-exec-$nonce
umask 077
mkdir -m 700 "$d" || exit 125
jobs=''
cleanup() {
  trap - EXIT HUP INT TERM
  for pid in $jobs; do kill "$pid" 2>/dev/null || :; done
  for pid in $jobs; do wait "$pid" 2>/dev/null || :; done
  rm -f "$d/out" "$d/err" "$d/out-hash" "$d/err-hash" "$d/out-sum" "$d/err-sum" "$d/ack-new" "$d/ack" "$d/owner"
  rmdir "$d" 2>/dev/null || :
}
trap cleanup EXIT
trap 'exit 125' HUP INT TERM
printf '%s\n' "$owner" > "$d/owner" || exit 125
mkfifo "$d/out" "$d/err" "$d/out-hash" "$d/err-hash" || exit 125
sha256sum < "$d/out-hash" > "$d/out-sum" &
oh=$!; jobs="$jobs $oh"
sha256sum < "$d/err-hash" > "$d/err-sum" &
eh=$!; jobs="$jobs $eh"
tee "$d/out-hash" < "$d/out" &
ot=$!; jobs="$jobs $ot"
tee "$d/err-hash" < "$d/err" >&2 &
et=$!; jobs="$jobs $et"
"$@" > "$d/out" 2> "$d/err"
status=$?
await_job() {
  wait "$1"
  result=$?
  active=''
  for pid in $jobs; do [ "$pid" = "$1" ] || active="$active $pid"; done
  jobs=$active
  return "$result"
}
await_job "$ot" || exit 125
await_job "$et" || exit 125
await_job "$oh" || exit 125
await_job "$eh" || exit 125
read -r out_hash unused < "$d/out-sum" || exit 125
read -r err_hash unused < "$d/err-sum" || exit 125
case "$out_hash$err_hash" in *[!0-9a-f]*) exit 125;; esac
[ "${#out_hash}" -eq 64 ] && [ "${#err_hash}" -eq 64 ] || exit 125
printf '\036PODGROVE-EXEC:%s:O:%03d:%s\037' "$nonce" "$status" "$out_hash"
printf '\036PODGROVE-EXEC:%s:E:%03d:%s\037' "$nonce" "$status" "$err_hash" >&2
n=0
while [ ! -f "$d/ack" ]; do
  [ "$n" -lt 1200 ] && [ -d "$d" ] || exit 125
  sleep 0.1
  n=$((n + 1))
done
[ ! -L "$d/ack" ] || exit 125
read -r ack < "$d/ack" || exit 125
[ "$ack" = "$nonce" ] || exit 125
exit 0
'''
_ACK = r'''
nonce=$1
owner=$2
case "$nonce$owner" in *[!0-9a-f]*|'') exit 125;; esac
[ "${#nonce}" -eq 32 ] && [ "${#owner}" -eq 32 ] || exit 125
d=/tmp/podgrove-exec-$nonce
[ -d "$d" ] && [ ! -L "$d" ] || exit 125
[ -f "$d/owner" ] && [ ! -L "$d/owner" ] || exit 125
read -r actual < "$d/owner" || exit 125
[ "$actual" = "$owner" ] || exit 125
umask 077
set -C
printf '%s\n' "$nonce" > "$d/ack-new" && mv "$d/ack-new" "$d/ack"
'''
_CLEANUP = r'''
nonce=$1
owner=$2
case "$nonce$owner" in *[!0-9a-f]*|'') exit 125;; esac
[ "${#nonce}" -eq 32 ] && [ "${#owner}" -eq 32 ] || exit 125
d=/tmp/podgrove-exec-$nonce
[ ! -L "$d" ] || exit 125
[ -d "$d" ] || exit 0
[ -f "$d/owner" ] && [ ! -L "$d/owner" ] || exit 125
read -r actual < "$d/owner" || exit 125
[ "$actual" = "$owner" ] || exit 125
rm -f "$d/out" "$d/err" "$d/out-hash" "$d/err-hash" "$d/out-sum" "$d/err-sum" "$d/ack-new" "$d/ack" "$d/owner"
rmdir "$d"
'''


def _incomplete():
    return PodgroveError("Exec output completion could not be verified; output may be incomplete. "
                         "The command was not retried; verify its effects before running it again")


class _OutputProof:
    """Stream application bytes immediately; retain only a possible fixed trailer."""

    def __init__(self, nonce, channel, write):
        self.prefix = b"\x1ePODGROVE-EXEC:" + nonce.encode("ascii") + b":" + channel + b":"
        self.pattern = re.compile(re.escape(self.prefix) + rb"([0-9]{3}):([0-9a-f]{64})\x1f")
        self.trailer_size = len(self.prefix) + 3 + 1 + 64 + 1
        self.pending = bytearray()
        self.digest = hashlib.sha256()
        self.write = write
        self.status = None
        self.candidate = b""
        self.sealed = False

    def _emit(self, data):
        if data:
            self.digest.update(data)
            self.write(data)

    def feed(self, chunk):
        if self.status is not None:
            if chunk:
                if self.sealed:
                    raise _incomplete()
                self.status = None
                self._emit(self.candidate)
                self.candidate = b""
            else:
                return
        self.pending.extend(chunk)
        while self.pending:
            offset = self.pending.find(self.prefix)
            if offset < 0:
                # Retain only a suffix that could be the beginning of a trailer;
                # ordinary short log lines are never delayed until command exit.
                keep = 0
                for size in range(min(len(self.pending), len(self.prefix) - 1), 0, -1):
                    if self.pending.endswith(self.prefix[:size]):
                        keep = size
                        break
                count = len(self.pending) - keep
                self._emit(bytes(self.pending[:count]))
                del self.pending[:count]
                return
            if offset:
                self._emit(bytes(self.pending[:offset]))
                del self.pending[:offset]
            if len(self.pending) < self.trailer_size:
                return
            match = self.pattern.match(self.pending)
            if match is None:
                self._emit(bytes(self.pending[:1]))
                del self.pending[:1]
                continue
            status = int(match[1])
            if status > 255 or match[2].decode("ascii") != self.digest.hexdigest():
                # Marker-shaped application bytes are ordinary output. A
                # damaged actual trailer will still fail completion at EOF.
                if len(self.pending) == match.end():
                    return
                self._emit(bytes(self.pending[:1]))
                del self.pending[:1]
                continue
            if len(self.pending) != match.end():
                self._emit(bytes(self.pending[:match.end()]))
                del self.pending[:match.end()]
                continue
            self.candidate = bytes(self.pending)
            self.pending.clear()
            self.status = status

    def finish(self):
        if self.status is None:
            # Do not leak a recognizable control trailer into a binary export.
            if not self.pending.startswith(self.prefix):
                self._emit(bytes(self.pending))
            self.pending.clear()
            raise _incomplete()


def _destination(destination, default_fd):
    if destination == subprocess.DEVNULL:
        return None
    fd = default_fd if destination is None else destination if isinstance(destination, int) else destination.fileno()
    if type(fd) is not int or fd < 0:
        raise PodgroveError("Exec output needs an open descriptor or DEVNULL; PIPE capture is unsupported")
    os.fstat(fd)
    return fd


class _Sink:
    def __init__(self, fd):
        self.pending = bytearray()
        self.process = None
        self.closed = False
        if fd is not None:
            # Own the blocking writer so cancellation never leaves an unkillable
            # thread or changes flags on the caller's shared file description.
            self.process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-B", "-c", _SINK_PROGRAM],
                stdin=subprocess.PIPE, stdout=fd, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                os.set_blocking(self.process.stdin.fileno(), False)
            except BaseException:
                try:
                    _stop(self.process)
                finally:
                    self.process.stdin.close()
                raise

    @property
    def fd(self):
        return self.process.stdin.fileno() if self.process is not None and not self.closed else None

    def append(self, data):
        if self.process is not None:
            self.pending.extend(data)

    def flush(self):
        try:
            count = os.write(self.fd, self.pending[:65536])
        except (BlockingIOError, InterruptedError):
            return
        if not count:
            raise _incomplete()
        del self.pending[:count]

    def finish(self):
        if self.process is not None and not self.closed:
            self.process.stdin.close()
        self.closed = True

    def done(self):
        if self.process is None:
            return True
        result = self.process.poll()
        if result is not None and result != 0:
            raise _incomplete()
        return result == 0

    def close(self):
        if self.process is not None:
            try:
                _stop(self.process)
            finally:
                self.process.stdin.close()


_SINK_PROGRAM = '''import os, select
while True:
    block = os.read(0, 65536)
    if not block:
        break
    remaining = memoryview(block)
    while remaining:
        try:
            count = os.write(1, remaining)
        except BlockingIOError:
            select.select([], [1], [])
            continue
        except InterruptedError:
            continue
        if not count:
            raise SystemExit(1)
        remaining = remaining[count:]
'''


def _relay(process, nonce, stdout_fd, stderr_fd, acknowledge, cancelled):
    sinks = {}
    acknowledged = False
    deadline = None
    try:
        for fd in (stdout_fd, stderr_fd):
            if fd not in sinks:
                sinks[fd] = _Sink(fd)
        output = _OutputProof(nonce, b"O", sinks[stdout_fd].append)
        error = _OutputProof(nonce, b"E", sinks[stderr_fd].append)
        channels = {process.stdout: (output, sinks[stdout_fd]), process.stderr: (error, sinks[stderr_fd])}
        while channels or any(sink.pending for sink in sinks.values()):
            if cancelled():
                raise _incomplete()
            if process.poll() is not None and deadline is None:
                deadline = time.monotonic() + EXIT_DRAIN_TIMEOUT
            if deadline is not None and time.monotonic() >= deadline:
                raise _incomplete()
            reads = [pipe for pipe, (_, sink) in channels.items()
                     if len(sink.pending) < OUTPUT_BUFFER_BYTES - 65536 - 256]
            for sink in sinks.values():
                sink.done()  # Detect a broken output consumer without waiting for input EOF.
            writes = {sink.fd: sink for sink in sinks.values() if sink.pending}
            readable, writable, _ = select.select(reads, writes, [], .1)
            for fd in writable:
                writes[fd].flush()
            for pipe in readable:
                proof, sink = channels[pipe]
                if len(sink.pending) >= OUTPUT_BUFFER_BYTES - 65536 - 256:
                    continue
                chunk = os.read(pipe.fileno(), 65536)
                if chunk:
                    proof.feed(chunk)
                else:
                    del channels[pipe]
                    proof.finish()
            if (not acknowledged and output.status is not None and error.status is not None
                    and not any(sink.pending for sink in sinks.values())):
                if output.status != error.status:
                    raise _incomplete()
                output.sealed = error.sealed = True
                for sink in sinks.values():
                    sink.finish()
                if all(sink.done() for sink in sinks.values()):
                    acknowledge()
                    acknowledged = True
                    deadline = time.monotonic() + EXIT_DRAIN_TIMEOUT
        if not acknowledged or process.wait(timeout=EXIT_DRAIN_TIMEOUT) != 0:
            raise _incomplete()
        return output.status
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _incomplete() from exc
    finally:
        failure = sys.exception()
        cleanup_failed = False
        for sink in sinks.values():
            try:
                sink.close()
            except (OSError, subprocess.TimeoutExpired):
                if failure is None:
                    cleanup_failed = True
                else:
                    failure.add_note("Exec output helper cleanup could not be confirmed")
        process.stdout.close()
        process.stderr.close()
        if cleanup_failed:
            raise _incomplete()


def _environment():
    # SPDY has produced successful exits with incomplete output on real clusters.
    # Select before executing; never change transport and replay a failed exec.
    return {**os.environ, "KUBECTL_REMOTE_COMMAND_WEBSOCKETS": "true"}


def _stop(process, *, group=True):
    if process.poll() is not None:
        if group:
            # An exited kubectl leader can leave an auth helper holding pipes.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        return
    try:
        if group:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            if group:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
    if group:
        # Reap helpers even when the group leader honored SIGTERM first.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _read(command, *, cancel=None, limit=1024 * 1024, timeout=6):
    """Do not expose metadata, argv or authentication-plugin stderr on failure."""
    if cancel is not None and cancel.is_set():
        raise PodgroveError("Exec ownership read was cancelled")
    try:
        process = subprocess.Popen(command, env=_environment(), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise PodgroveError("Exec discovery could not start kubectl") from exc
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(process.stdout, selectors.EVENT_READ)
            while poll.get_map():
                if cancel is not None and cancel.is_set():
                    raise PodgroveError("Exec ownership read was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PodgroveError("Exec discovery timed out; command was not retried")
                for key, _ in poll.select(min(remaining, .1)):
                    chunk = os.read(key.fd, min(16384, limit + 1 - len(output)))
                    if not chunk:
                        poll.unregister(key.fileobj)
                    output.extend(chunk)
                    if len(output) > limit:
                        raise PodgroveError("Exec discovery response exceeded its size limit")
        # EOF does not imply that kubectl exited. Keep cancellation responsive
        # while an authentication plugin or transport cleanup still holds it.
        while process.poll() is None:
            if cancel is not None and cancel.is_set():
                raise PodgroveError("Exec ownership read was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PodgroveError("Exec discovery timed out; command was not retried")
            try:
                process.wait(timeout=min(remaining, .1))
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise PodgroveError("Exec discovery failed; check engine readiness and read permissions")
        return bytes(output)
    except subprocess.TimeoutExpired as exc:
        raise PodgroveError("Exec discovery timed out; command was not retried") from exc
    finally:
        _stop(process)
        process.stdout.close()


def _engine(kube, ident, *, cancel=None):
    resources = []
    for kind, name in (("statefulset", "pg-" + ident), ("pod", engine_pod_name(ident)),
                       ("persistentvolumeclaim", "pg-" + ident)):
        if cancel is not None and cancel.is_set():
            raise PodgroveError("Exec ownership read was cancelled")
        try:
            raw = _read(kube.command("get", kind, name, "-o", "json", "--ignore-not-found"),
                        cancel=cancel, timeout=REQUEST_PROCESS_TIMEOUT)
            resource = json.loads(raw) if raw.strip() else {}
            meta = resource.get("metadata", {})
            labels = meta.get("labels", {})
            if (meta.get("name") != name or meta.get("namespace") != kube.namespace
                    or not isinstance(meta.get("uid"), str) or not meta["uid"] or meta.get("deletionTimestamp")
                    or labels.get(MANAGED) != "podgrove" or labels.get(ENVIRONMENT) != ident):
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise PodgroveError("Exec engine resources are missing, deleting, or foreign") from exc
        resources.append(resource)
    controller, pod, pvc = resources
    Kube._validate_pod_controller(pod, controller, ident)
    claims = [volume["persistentVolumeClaim"].get("claimName")
              for volume in pod.get("spec", {}).get("volumes", []) if "persistentVolumeClaim" in volume]
    if claims != ["pg-" + ident]:
        raise PodgroveError("Exec engine does not use its owned PVC")
    containers = [container for container in pod.get("spec", {}).get("containers", [])
                  if container.get("name") == "docker"]
    fields = [entry for container in containers for entry in container.get("env", [])
              if entry.get("name") == POD_UID_ENV]
    if (len(containers) != 1 or len(fields) != 1 or "value" in fields[0]
            or fields[0].get("valueFrom") != {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}}):
        raise PodgroveError("Exec needs the engine's verified Pod UID binding; run podgrove up --refresh")
    return tuple(resource["metadata"]["uid"] for resource in resources)


def _command(kube, ident, pod_uid, arguments, *, interactive=False, tty=False):
    return _guarded(kube, ident, pod_uid,
                    ["docker", "--host=unix:///var/run/docker.sock", *arguments], interactive=interactive, tty=tty)


def _guarded(kube, ident, pod_uid, arguments, *, interactive=False, tty=False):
    return kube.command("exec", "--request-timeout=0", *(["-i"] if interactive else []),
                        *(["-t"] if tty else []), engine_pod_name(ident), "-c", "docker", "--",
                        "sh", "-c", _UID_GUARD, "podgrove-exec-guard", pod_uid, *arguments)


def _container(kube, ident, pod_uid, project, service, index):
    expected = {PROJECT_LABEL: project, SERVICE_LABEL: service, NUMBER_LABEL: str(index), ONEOFF_LABEL: "False"}
    filters = [arg for key, value in expected.items() for arg in ("--filter", f"label={key}={value}")]
    raw = _read(_command(kube, ident, pod_uid, ["ps", "--no-trunc", *filters, "--format", "{{.ID}}"]), limit=65536)
    ids = raw.decode("utf-8", errors="replace").splitlines()
    if len(ids) != 1 or not re.fullmatch(r"[a-f0-9]{64}", ids[0]):
        raise PodgroveError("Exec requires exactly one running container for the recorded project, service and replica")
    container = ids[0]
    # Select by exact ID, then independently verify every label and running state.
    # A same-name replacement cannot silently receive the user's command.
    raw = _read(_command(kube, ident, pod_uid, ["inspect", "--format",
                "{{.Id}}\n{{.State.Running}}\n{{json .Config.Labels}}", container]), limit=65536)
    try:
        observed_id, running, labels = raw.decode("utf-8").strip().split("\n", 2)
        labels = json.loads(labels)
        if observed_id != container or running != "true" or any(labels.get(key) != value for key, value in expected.items()):
            raise ValueError()
    except (ValueError, UnicodeError, AttributeError) as exc:
        raise PodgroveError("Exec container ownership changed during discovery") from exc
    return container


def run_exec(kube, ident, project, service, arguments, *, tty=False, index=1,
             stdin=None, stdout=None, stderr=None, verification_interval=VERIFY_INTERVAL):
    """Execute once in replica 1, retaining its configured user/env/working dir."""
    if (not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{12}", ident)
            or not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project)
            or not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service)
            or type(index) is not int or index < 1 or verification_interval <= 0
            or not arguments or any(not isinstance(value, str) or "\0" in value for value in arguments)):
        raise PodgroveError("Invalid exec target or command")
    # Reject unsupported destinations before the uncertain user command starts.
    stdout_fd = _destination(stdout, 1) if not tty else None
    stderr_fd = (stdout_fd if stderr == subprocess.STDOUT else _destination(stderr, 2)) if not tty else None
    uids = _engine(kube, ident)
    container = _container(kube, ident, uids[1], project, service, index)
    if _engine(kube, ident) != uids:
        raise PodgroveError("Exec engine was replaced during discovery; command was not started")
    docker_args = ["docker", "--host=unix:///var/run/docker.sock", "exec", "-i",
                   *(["-t"] if tty else []), container, *arguments]
    nonce = None if tty else uuid.uuid4().hex
    owner = None if tty else secrets.token_hex(16)
    remote_args = docker_args if tty else ["sh", "-c", _STREAM_WRAPPER, "podgrove-exec-stream", nonce, owner, *docker_args]
    command = _guarded(kube, ident, uids[1], remote_args, interactive=True, tty=tty)
    try:
        process = subprocess.Popen(command, env=_environment(), stdin=stdin,
                                   stdout=stdout if tty else subprocess.PIPE,
                                   stderr=stderr if tty else subprocess.PIPE,
                                   start_new_session=not tty)
    except OSError as exc:
        raise PodgroveError("Could not start exec transport; command was not retried") from exc
    stopped = threading.Event()
    failures = []

    def verify():
        while not stopped.wait(verification_interval):
            try:
                if _engine(kube, ident, cancel=stopped) != uids:
                    raise PodgroveError("Exec engine Pod, StatefulSet or PVC was replaced")
            except Exception:
                if not stopped.is_set():
                    failures.append(True)
                    _stop(process, group=not tty)
                return

    monitor = threading.Thread(target=verify, name="podgrove-exec-ownership", daemon=True)
    complete = False

    def acknowledge():
        # Separate protocol bookkeeping preserves true EOF on application stdin.
        # The guard binds this one write to the same Pod, never a replacement.
        try:
            if _read(_guarded(kube, ident, uids[1], ["sh", "-c", _ACK, "podgrove-exec-ack", nonce, owner])):
                raise _incomplete()
        except (PodgroveError, OSError, subprocess.TimeoutExpired) as exc:
            raise _incomplete() from exc

    try:
        monitor.start()
        result = process.wait() if tty else _relay(process, nonce, stdout_fd, stderr_fd, acknowledge, lambda: bool(failures))
        stopped.set()
        monitor.join(timeout=3)
        try:
            still_owned = _engine(kube, ident) == uids
        except Exception:
            still_owned = False
        if monitor.is_alive() or failures or not still_owned:
            raise PodgroveError("Exec ownership changed or could not be verified; output may be incomplete. "
                                "The command was not retried; verify its effects before running it again")
        complete = True
        return result
    finally:
        stopped.set()
        failure = sys.exception()
        try:
            try:
                _stop(process, group=not tty)
            except (OSError, subprocess.TimeoutExpired):
                if failure is None:
                    raise _incomplete()
                failure.add_note("Exec transport cleanup could not be confirmed")
        finally:
            if monitor.ident is not None:
                monitor.join(timeout=3)
            if nonce is not None and not complete:
                # Only this command's fixed nonce directory is removed. Failure
                # is best effort: the wrapper also has traps and an ACK timeout.
                try:
                    _read(_guarded(kube, ident, uids[1],
                                   ["sh", "-c", _CLEANUP, "podgrove-exec-cleanup", nonce, owner]))
                except Exception:
                    pass
