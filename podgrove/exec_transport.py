"""Stream exec through one owned Kubernetes exec, without nested HTTP hijacking.

An interrupted command is never replayed: it may already have changed state.
Only bounded discovery reads are captured; command stdin/stdout/stderr stream
directly between the terminal and kubectl's WebSocket remote-command transport.
"""
from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time

from .docker_tunnel import POD_UID_ENV, _UID_GUARD
from .errors import PodgroveError
from .kube import ENVIRONMENT, MANAGED, REQUEST_PROCESS_TIMEOUT, Kube, engine_pod_name

VERIFY_INTERVAL = 5.0
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
NUMBER_LABEL = "com.docker.compose.container-number"
ONEOFF_LABEL = "com.docker.compose.oneoff"


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
            except ProcessLookupError:
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
        except ProcessLookupError:
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
    return kube.command("exec", "--request-timeout=0", *(["-i"] if interactive else []),
                        *(["-t"] if tty else []), engine_pod_name(ident), "-c", "docker", "--",
                        "sh", "-c", _UID_GUARD, "podgrove-exec-guard", pod_uid,
                        "docker", "--host=unix:///var/run/docker.sock", *arguments)


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
    uids = _engine(kube, ident)
    container = _container(kube, ident, uids[1], project, service, index)
    if _engine(kube, ident) != uids:
        raise PodgroveError("Exec engine was replaced during discovery; command was not started")
    command = _command(kube, ident, uids[1], ["exec", "-i", *(["-t"] if tty else []), container, *arguments],
                       interactive=True, tty=tty)
    try:
        process = subprocess.Popen(command, env=_environment(), stdin=stdin, stdout=stdout, stderr=stderr,
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
    try:
        monitor.start()
        result = process.wait()
        stopped.set()
        monitor.join(timeout=3)
        try:
            still_owned = _engine(kube, ident) == uids
        except Exception:
            still_owned = False
        if monitor.is_alive() or failures or not still_owned:
            raise PodgroveError("Exec ownership changed or could not be verified; output may be incomplete. "
                                "The command was not retried; verify its effects before running it again")
        return result
    finally:
        stopped.set()
        _stop(process, group=not tty)
        if monitor.ident is not None:
            monitor.join(timeout=3)
