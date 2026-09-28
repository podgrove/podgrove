"""Stack launch and local session supervision."""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import state
from .activity import WatchActivity
from .compose import Compose
from .config import load_config
from .docker_tunnel import DockerTunnel
from .errors import PodgroveError
from .forward import PortMappingError, Tunnel, free_port, port_plan, verify_port_mappings
from .kube import HeartbeatUnavailable, Kube
from .process import docker_environment, run
from .reaper import reason
from .sync import SnapshotRace, Synchronizer
from .sync_recovery import SyncRecoveryUnavailable, verify_engine as verify_sync_engine
from .sync_transport import SyncStreamError

HEALTH_INTERVAL = 30.0
HEARTBEAT_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
STATUS_RETRY_DELAYS = (0.2, 0.5)
STATUS_READ_TIMEOUT = 10
SYNC_RETRY_DELAYS = (0.25, 1.0, 2.0)
_TRANSIENT_READ = re.compile(
    r"connection reset by peer|connection refused|unexpected eof|\beof\b|broken pipe|"
    r"i/o timeout|docker timed out after", re.IGNORECASE)


class TransientDockerReadError(PodgroveError):
    """A read-only Docker observation exhausted its bounded retry budget."""


class SessionStopped(Exception):
    """Authenticated cancellation of an in-progress session operation."""


class SyncWorker:
    """One serialized sync loop, independent of slower Kubernetes/health reads."""

    def __init__(self, sync, watch_activity, interval=0.4, on_status=None, retry_delays=None):
        self.sync, self.watch_activity, self.interval = sync, watch_activity, interval
        self.stopping = threading.Event()
        self.cycle = threading.Lock()
        self._lock = threading.Lock()
        self._activity = 0.0
        self._error = None
        self.on_status = on_status
        self.retry_delays = tuple(SYNC_RETRY_DELAYS if retry_delays is None else retry_delays)
        self._status = {"state": "ready", "error": None, "checked_at": time.time()}
        self.thread = threading.Thread(target=self._run, name="podgrove-file-sync", daemon=True)
        sync.activity_callback = self._touch

    def _touch(self):
        with self._lock:
            self._activity = time.time()

    def snapshot(self):
        with self._lock:
            if self._error is not None:
                raise self._error
            return self._activity

    def start(self):
        self.thread.start()
        return self

    def status(self):
        with self._lock:
            return dict(self._status)

    def _report(self, state_name, error=None, *, attempts=0, retry_in=None):
        with self._lock:
            changed = (self._status["state"] != state_name or self._status["error"] != error
                       or self._status.get("attempts", 0) != attempts)
            self._status = {"state": state_name, "error": error, "checked_at": time.time(),
                            "attempts": attempts, "next_retry_at": time.time() + retry_in if retry_in is not None else None}
            current = dict(self._status)
        if changed and self.on_status:
            self.on_status(current)

    def _run(self):
        races = 0
        reconnecting, disconnected = False, False
        attempts, recovered_at = 0, None
        try:
            while not self.stopping.is_set():
                delay = self.interval
                if disconnected:
                    # Never hold the cycle lock while paused: TTL shutdown
                    # must remain able to acquire it and stop this worker.
                    self.stopping.wait(delay)
                    continue
                try:
                    with self.cycle:
                        if self.stopping.is_set():
                            return
                        if self.watch_activity.changed():
                            self._touch()
                        if reconnecting:
                            attempts += 1
                            self._report("reconnecting", "File sync is reconnecting to the original engine", attempts=attempts)
                            self.sync.reconnect()
                            reconnecting, recovered_at = False, time.monotonic()
                        started = time.monotonic()
                        changed = self.sync.sync_once()
                        if changed:
                            self._touch()
                            timing = getattr(self.sync, "last_timing", {})
                            detail = ("; " + ", ".join(f"{name}={value:.3f}" for name, value in timing.items())
                                      if isinstance(timing, dict) and timing else "")
                            print(f"Bind sync: {changed} changed entries in {time.monotonic() - started:.3f}s{detail}", flush=True)
                    races = 0
                    if recovered_at is not None and time.monotonic() - recovered_at >= 30:
                        attempts, recovered_at = 0, None
                    self._report("ready", attempts=attempts)
                except (SyncStreamError, SyncRecoveryUnavailable) as exc:
                    if self.stopping.is_set():
                        return
                    retryable = isinstance(exc, SyncRecoveryUnavailable) or exc.reconnectable
                    if retryable and attempts < len(self.retry_delays):
                        reconnecting = True
                        delay = self.retry_delays[attempts]
                        self._report("reconnecting", "File sync transport unavailable; retrying the original engine",
                                     attempts=attempts, retry_in=delay)
                    else:
                        disconnected = True
                        self.sync.pause()
                        self._report("disconnected", "File sync paused: " + str(exc)
                                     + ". No batch was replayed; inspect the mirror and run podgrove up --refresh to reconnect",
                                     attempts=attempts)
                except SnapshotRace as exc:
                    # This type is raised only during local preparation, before
                    # remote bytes/ACK ambiguity. Keep existing forwards alive.
                    if not self.stopping.is_set():
                        self._touch()
                        self._report("retrying", str(exc))
                    races += 1
                    delay = min(2.0, self.interval * 2 ** min(races, 4))
                self.stopping.wait(delay)
        except BaseException as exc:
            if not self.stopping.is_set():
                with self._lock:
                    self._error = exc

    def close(self):
        self.stopping.set()
        self.sync.cancel()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise PodgroveError("File sync worker did not stop; refusing concurrent helper cleanup")


def service_status(compose: Compose, env: dict) -> list[dict]:
    # Only this read is replayable. Build/up/exec and sync transfers can mutate
    # remote state and must never inherit this retry policy.
    for attempt in range(len(STATUS_RETRY_DELAYS) + 1):
        try:
            output = run(compose.command("ps", "--all", "--format", "json"), env=env,
                         cwd=compose.config.root, timeout=STATUS_READ_TIMEOUT).stdout.strip()
            break
        except PodgroveError as exc:
            if not _TRANSIENT_READ.search(str(exc)):
                raise
            if attempt == len(STATUS_RETRY_DELAYS):
                raise TransientDockerReadError(
                    f"Docker status temporarily unavailable after {attempt + 1} read attempts; "
                    "retry status. The read did not stop the session.") from exc
            time.sleep(STATUS_RETRY_DELAYS[attempt])
    if not output:
        return []
    try:
        parsed = json.loads(output)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        return [json.loads(line) for line in output.splitlines() if line.strip()]


def readiness(model: dict, rows: list[dict]) -> tuple[bool, list[str]]:
    jobs = {name for svc in model["services"].values() for name, dependency in svc.get("depends_on", {}).items()
            if isinstance(dependency, dict) and dependency.get("condition") == "service_completed_successfully"}
    problems = []
    for name, svc in model["services"].items():
        replicas = svc.get("deploy", {}).get("replicas", svc.get("scale", 1))
        if replicas == 0:
            continue
        matches = [r for r in rows if r.get("Service") == name]
        if len(matches) < replicas:
            problems.append(f"{name}: {len(matches)}/{replicas} containers present")
        for row in matches:
            status, health = row.get("State", "unknown"), row.get("Health", "")
            if name in jobs and status == "exited" and int(row.get("ExitCode", 1)) == 0:
                continue
            if name in jobs:
                problems.append(f"{name}: waiting for successful completion ({status})")
                continue
            if status != "running" or health not in ("", "healthy"):
                problems.append(f"{name}: {status}" + (f"/{health}" if health else ""))
    return not problems, problems


def _remote_build_output(output: str) -> str:
    """Drop only Compose's misleading local Docker Desktop build-details link."""
    return "".join(line for line in output.splitlines(keepends=True) if not re.fullmatch(
        r"\s*View build details:\s+docker-desktop://dashboard/build/\S+\s*", line))


def _startup_phase(message: str) -> None:
    # Fixed phase labels only: never print configuration, credentials or argv.
    # The detached supervisor writes to a buffered file, so flush before work.
    print(f"Startup: {message}", flush=True)


def launch_stack(compose: Compose, model: dict, env: dict, ident: str, timeout: int = 600):
    sync = Synchronizer(compose.config.root, compose.sync_paths(model), env, ident,
                        exclude=getattr(compose.config, "sync_exclude", []))
    try:
        _startup_phase("copying the initial workspace snapshot")
        sync.start()
        started = time.monotonic()
        _startup_phase("building and starting Compose services")
        result = run(compose.command("up", "--detach", "--build"), env=env, cwd=compose.config.root, timeout=timeout)
        stdout, stderr = _remote_build_output(result.stdout), _remote_build_output(result.stderr)
        if stdout:
            print(stdout, flush=True)
        if stderr:
            print(stderr, file=sys.stderr, flush=True)
        deadline = time.monotonic() + timeout
        _startup_phase("waiting for Compose service readiness")
        while True:
            rows = service_status(compose, env)
            ready, problems = readiness(model, rows)
            if ready:
                return sync, time.monotonic() - started, rows
            if time.monotonic() >= deadline:
                raise PodgroveError("Stack not ready: " + "; ".join(problems))
            # Surface unrecoverable failures rather than waiting for the entire timeout.
            if any(r.get("State") == "exited" and int(r.get("ExitCode", 0)) != 0 for r in rows):
                raise PodgroveError("Stack container failed: " + "; ".join(problems))
            time.sleep(1)
    except BaseException:
        try:
            sync.close()
        except Exception as cleanup_error:
            print(f"Sync cleanup after stack failure: {cleanup_error}", file=sys.stderr, flush=True)
        raise


def failure_detail(error: BaseException, tunnels: list) -> str:
    """Preserve the primary failure and any independently recorded tunnel cause."""
    details = [str(error)]
    for tunnel in tunnels:
        try:
            tunnel.check()
        except Exception as cause:
            if str(cause) not in details:
                details.append(str(cause))
    return "; ".join(details)


def control(data: dict, action: str, timeout: float = 3) -> dict:
    if not isinstance(data.get("socket"), str) or not isinstance(data.get("token"), str):
        raise PodgroveError("Podgrove session has no usable control endpoint; run podgrove up to reconnect")
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(timeout)
            client.connect(data["socket"])
            client.sendall(json.dumps({"action": action, "token": data["token"]}).encode() + b"\n")
            payload = bytearray()
            while chunk := client.recv(min(4096, 65537 - len(payload))):
                payload.extend(chunk)
                if len(payload) > 65536:
                    raise PodgroveError("Session control response exceeded its size limit")
            response = json.loads(payload)
            if not isinstance(response, dict):
                raise PodgroveError("Invalid session control response; refusing the operation")
            return response
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise PodgroveError("Podgrove session is not connected; run podgrove up to reconnect") from exc


def is_running(data: dict) -> bool:
    try:
        return control(data, "ping").get("ok", False)
    except PodgroveError:
        return False


def stop_session(data: dict, timeout: float = 30) -> None:
    """Stop only an authenticated session and wait until it releases its socket."""
    connected = is_running(data)
    socket_path = Path(data["socket"]) if data.get("socket") else None
    if not connected:
        if socket_path and socket_path.exists():
            metadata = socket_path.lstat()
            if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.getuid():
                try:
                    with socket.socket(socket.AF_UNIX) as probe:
                        probe.settimeout(1)
                        probe.connect(str(socket_path))
                except (ConnectionRefusedError, FileNotFoundError):
                    return  # An owned stale socket is safe to remove after cleanup.
                except OSError:
                    pass
            raise PodgroveError("Session socket exists but cannot be authenticated; inspect its log before cleanup")
        return
    if not control(data, "stop").get("ok"):
        raise PodgroveError("Session refused authenticated shutdown; leaving environment state in place")
    deadline = time.monotonic() + timeout
    while socket_path and socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if socket_path and socket_path.exists():
        raise PodgroveError("Session is still shutting down; retry cleanup after it finishes")


def spawn(path: Path) -> None:
    log_path = path.with_suffix(".log")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as log:
        # Resolve the supervisor from this interpreter's installed packages, not
        # a checkout in cwd or PYTHONPATH. Keep the environment for Compose and
        # credential helpers; -I ignores Python's import settings, and -B avoids
        # bytecode writes even though -I ignores PYTHONDONTWRITEBYTECODE.
        subprocess.Popen([sys.executable, "-I", "-B", "-m", "podgrove", "_serve", str(path)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def serve(path: Path) -> int:
    data = state.read(path)
    state.validate_binding(data, Path(data.get("root", "")), data.get("context", ""))
    stopping = False
    tearing_down = False
    exit_code = 0
    last_touch = 0.0
    activity_lock = threading.RLock()
    serve_thread = threading.get_ident()
    def stop_signal(*_):
        nonlocal stopping
        stopping = True
        if not tearing_down:
            raise SessionStopped("Session cancelled")
    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    kube = Kube(data["context"], data["namespace"], namespace_mode=state.namespace_mode(data))
    tunnels = []
    sync = None
    watch = None
    server = socket.socket(socket.AF_UNIX)
    bound = False
    controller = None
    sync_worker = None
    app_tunnel = None
    api_tunnel = None
    services_ready = True

    def persist():
        with activity_lock:
            if api_tunnel is not None:
                data["docker_status"] = api_tunnel.snapshot()
                identity_snapshot = getattr(api_tunnel, "identity_snapshot", None)
                if callable(identity_snapshot):
                    identity = identity_snapshot()
                    if isinstance(identity, dict):
                        data["engine_identity"] = identity
            if app_tunnel is not None and not tearing_down:
                data["forward_status"] = app_tunnel.snapshot()
                for port in data.get("ports", []):
                    port["status"] = data["forward_status"]["state"]
            if sync_worker is not None:
                data["sync_status"] = sync_worker.status()
            session_health()
            state.write(path, data)

    def session_health():
        if data.get("status") not in ("ready", "unhealthy", "degraded"):
            return
        degraded = (data.get("forward_status", {}).get("state") in ("reconnecting", "disconnected")
                    or data.get("sync_status", {}).get("state") in ("retrying", "reconnecting", "disconnected")
                    or data.get("health_status", {}).get("state") == "unavailable"
                    or data.get("heartbeat_status", {}).get("state") == "unavailable"
                    or data.get("docker_status", {}).get("verification", {}).get("state") in ("unavailable", "expired"))
        data["status"] = "degraded" if degraded else "ready" if services_ready else "unhealthy"

    def forward_changed(current):
        with activity_lock:
            data["forward_status"] = current
            for port in data.get("ports", []):
                port["status"] = current["state"]
            if stopping or tearing_down:
                return
            session_health()
            persist()

    def sync_changed(current):
        with activity_lock:
            data["sync_status"] = current
            if stopping or tearing_down:
                return
            session_health()
            persist()

    def handle_control():
        nonlocal stopping, last_touch
        while not stopping:
            try:
                client, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                client.settimeout(2)
                cancel_session = False
                try:
                    message = json.loads(client.recv(4096))
                    if not isinstance(message, dict) or message.get("token") != data["token"]:
                        response = {"ok": False}
                    else:
                        action = message.get("action")
                        with activity_lock:
                            accepted = action in ("stop", "ping", "touch") and not stopping
                            if accepted and action == "stop":
                                stopping = True
                                cancel_session = True
                            elif accepted and action == "touch":
                                last_touch = time.time()
                            response = {"ok": accepted, "status": data["status"]}
                            if app_tunnel is not None:
                                response["forward_status"] = app_tunnel.snapshot()
                            elif "forward_status" in data:
                                response["forward_status"] = data["forward_status"]
                            if sync_worker is not None:
                                response["sync_status"] = sync_worker.status()
                            if "health_status" in data:
                                response["health_status"] = dict(data["health_status"])
                            if "heartbeat_status" in data:
                                response["heartbeat_status"] = dict(data["heartbeat_status"])
                            if api_tunnel is not None:
                                response["docker_status"] = api_tunnel.snapshot()
                    client.sendall(json.dumps(response).encode())
                except (OSError, ValueError):
                    pass
                if cancel_session and serve_thread == threading.main_thread().ident:
                    # Signal this authenticated supervisor, never an unverified persisted PID.
                    # subprocess.run kills/waits its child when the handler interrupts a check.
                    os.kill(os.getpid(), signal.SIGTERM)
    try:
        # Unique token in pathname prevents stale-PID and stale-socket adoption.
        server.bind(data["socket"])
        bound = True
        os.chmod(data["socket"], 0o600)
        server.listen(4)
        server.settimeout(0.2)
        data["pid"] = os.getpid()
        persist()
        controller = threading.Thread(target=handle_control, name="podgrove-control", daemon=True)
        controller.start()
        _startup_phase("loading and validating Compose configuration")
        config = load_config(state.configuration_root(data), Path(data["config_path"]) if data.get("config_path") else None,
                             data.get("files"))
        compose = Compose(config)
        model = compose.model()
        compose.validate(model)
        _startup_phase("waiting for engine Pod readiness")
        kube.wait(data["identity"], data["timeout"])
        api_port = free_port()
        _startup_phase("opening the Docker API connection")
        api_tunnel = DockerTunnel(kube, data["identity"], api_port).start()
        tunnels.append(api_tunnel)
        persist()  # Capture immutable engine UIDs before info, initial sync or a long build.
        env = docker_environment(f"tcp://127.0.0.1:{api_port}")
        # Explicitly target the engine throughout. Never select or mutate a Docker context.
        _startup_phase("checking Docker engine readiness")
        run(["docker", "info"], env=env, timeout=30)
        sync, elapsed, rows = launch_stack(compose, model, env, data["identity"], data["timeout"])
        # Capture the original engine proof, not a later same-name replacement.
        # The sync event cancels these reads before the worker is joined.
        captured_identity = getattr(api_tunnel, "identity_snapshot", lambda: {})()
        expected_sync_uids = captured_identity.get("expected") if isinstance(captured_identity, dict) else None
        sync.reconnect_guard = lambda cancelled, **kwargs: verify_sync_engine(
            kube, data["identity"], expected_sync_uids, cancelled, **kwargs)
        ports = port_plan(compose.published_ports(model), config.forward, data["identity"],
                          observed=rows, project=model.get("name"))
        if ports:
            _startup_phase("opening application port forwards")
            app_tunnel = Tunnel(kube, data["identity"], [(p["local"], p["published"]) for p in ports])
            app_tunnel.on_change = forward_changed
            tunnels.append(app_tunnel.start())
            for port in ports:
                port["status"] = "ready"
        else:
            data["forward_status"] = {"state": "disabled", "error": None, "attempts": 0,
                                      "changed_at": time.time(), "checked_at": time.time()}
        if compose.has_watch(model):
            _startup_phase("starting Compose watch")
            watch = subprocess.Popen(compose.command("watch", "--no-up"), env=env, cwd=config.root,
                                     stdin=subprocess.DEVNULL)
        watch_activity = WatchActivity(model)
        with activity_lock:
            data.update({"status": "ready", "pid": os.getpid(), "docker_host": env["DOCKER_HOST"],
                         "ports": ports, "startup_seconds": round(elapsed, 3), "last_activity": time.time(),
                         "services": rows})
            data["sync_status"] = {"state": "ready", "error": None, "checked_at": time.time()}
            data["health_status"] = {"state": "ready", "checked_at": time.time(),
                                     "last_success_at": time.time()}
            session_health()
            persist()
        _startup_phase("ready")
        sync_worker = SyncWorker(sync, watch_activity, on_status=sync_changed)
        sync_worker.start()
        last_heartbeat = 0.0
        heartbeat_activity = 0.0
        heartbeat_retry_at = 0.0
        heartbeat_failures = 0
        last_health = time.monotonic()
        while not stopping:
            for tunnel in tunnels:
                tunnel.check()
            if watch and watch.poll() is not None:
                raise PodgroveError("docker compose watch exited; see the session log and run podgrove up")
            data["last_activity"] = max(data["last_activity"], last_touch, sync_worker.snapshot())
            activity_changed = data["last_activity"] > heartbeat_activity
            if (activity_changed or time.monotonic() - last_heartbeat >= min(30, config.ttl_seconds / 3)
                    or heartbeat_failures and time.monotonic() >= heartbeat_retry_at):
                why = reason(data, check_mr=False)
                if not why and data.get("mr_url"):
                    try:
                        why = reason(data)
                    except PodgroveError as exc:
                        # An unavailable GitLab must not disconnect a working session.
                        print(f"Lifecycle check: {exc}", file=sys.stderr, flush=True)
                if why:
                    # Finish an in-flight transfer and recheck activity while
                    # preventing a new sync cycle from racing TTL deletion.
                    with sync_worker.cycle, activity_lock:
                        data["last_activity"] = max(data["last_activity"], last_touch, sync_worker.snapshot())
                        if why == "idle TTL expired":
                            why = reason(data, check_mr=False)
                        if why and not stopping:
                            sync_worker.stopping.set()
                            data["status"] = "reaping"
                            data["reason"] = why
                            persist()
                            stopping = True
                    continue
                if stopping:
                    break
                if time.monotonic() >= heartbeat_retry_at:
                    try:
                        kube.heartbeat(data["identity"], data["last_activity"])
                    except HeartbeatUnavailable:
                        heartbeat_failures = min(heartbeat_failures + 1, 2**31 - 1)
                        delay = HEARTBEAT_RETRY_DELAYS[min(heartbeat_failures - 1, len(HEARTBEAT_RETRY_DELAYS) - 1)]
                        heartbeat_retry_at = time.monotonic() + delay
                        data["heartbeat_status"] = {
                            **data.get("heartbeat_status", {}), "state": "unavailable", "checked_at": time.time(),
                            "consecutive_failures": heartbeat_failures, "next_retry_at": time.time() + delay,
                            "error": "Lease heartbeat unavailable; cluster activity timestamp may be stale"}
                    else:
                        heartbeat_failures = 0
                        heartbeat_retry_at = 0.0
                        data["heartbeat_status"] = {"state": "ready", "checked_at": time.time(),
                                                    "last_success_at": time.time(), "consecutive_failures": 0}
                heartbeat_activity = data["last_activity"]
                if stopping:
                    break
                if time.monotonic() - last_health >= HEALTH_INTERVAL:
                    try:
                        rows = service_status(compose, env)
                    except TransientDockerReadError as exc:
                        # A failed observation does not invalidate the engine or
                        # an acknowledged sync. Keep existing services/forwards
                        # usable and identify the retained rows as stale.
                        data["health_status"] = {
                            **data.get("health_status", {}), "state": "unavailable",
                            "checked_at": time.time(), "error": str(exc)}
                        data["problems"] = [str(exc)]
                    else:
                        data["services"] = rows
                        if app_tunnel is not None:
                            try:
                                verify_port_mappings(ports, rows, model.get("name"))
                            except PortMappingError as exc:
                                # A recreated container can have a different
                                # Docker-assigned port. Never keep a ready
                                # endpoint aimed at that old port. Retain the
                                # engine and sync, but require explicit refresh.
                                app_tunnel.close()
                                with activity_lock:
                                    app_tunnel = None
                                    data["forward_status"] = {
                                        "state": "disconnected", "attempts": 0,
                                        "changed_at": time.time(), "checked_at": time.time(),
                                        "error": f"{exc} Local forwarding stopped; run podgrove up --refresh to reconnect."}
                                    for port in ports:
                                        port["status"] = "disconnected"
                        ready, problems = readiness(model, rows)
                        services_ready = ready
                        data["health_status"] = {"state": "ready", "checked_at": time.time(),
                                                 "last_success_at": time.time()}
                        data["problems"] = problems
                    sync_worker.snapshot()  # A slow health result must not mask sync failure.
                    if stopping:
                        break
                    with activity_lock:
                        session_health()
                    last_health = time.monotonic()
                data["last_activity"] = max(data["last_activity"], last_touch, sync_worker.snapshot())
                persist()
                last_heartbeat = time.monotonic()
            time.sleep(0.4)
        data["last_activity"] = max(last_touch, data["last_activity"], sync_worker.snapshot())
        if data["status"] != "reaping":
            data["status"] = "disconnected"
    except SessionStopped:
        data["status"] = "disconnected"
    except BaseException as exc:
        # A build can fail before the periodic ownership check notices a Pod
        # replacement. A fresh read explains the failure without repeating any
        # build, upload, exec or other uncertain Docker mutation.
        identity_failure = None
        refresh_identity = getattr(api_tunnel, "refresh_identity", None)
        if callable(refresh_identity):
            try:
                refresh_identity()
            except Exception as cause:
                identity_failure = str(cause)
        detail = failure_detail(exc, tunnels)
        if identity_failure and identity_failure not in detail:
            detail += "; " + identity_failure
        data.update({"status": "error", "error": detail})
        print(detail, file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        stopping = True
        tearing_down = True

        def cleanup_failure(label, error):
            nonlocal exit_code
            detail = f"{label}: {error}"
            primary = data.get("error") if data.get("status") == "error" else None
            data.update(status="error", error=f"{primary}; {detail}" if primary else detail)
            print(detail, file=sys.stderr, flush=True)
            exit_code = 1

        if watch and watch.poll() is None:
            watch.terminate()
            try:
                watch.wait(timeout=5)
            except subprocess.TimeoutExpired:
                watch.kill()
                watch.wait(timeout=5)
        sync_stopped = True
        if sync_worker:
            try:
                sync_worker.close()
            except Exception as exc:
                sync_stopped = False
                cleanup_failure("Sync worker cleanup", exc)
        if sync and sync_stopped:
            try:
                sync.close()
            except Exception as exc:
                cleanup_failure("Sync cleanup", exc)
        for tunnel in reversed(tunnels):
            try:
                tunnel.close()
            except Exception as exc:
                cleanup_failure("Tunnel cleanup", exc)
        with activity_lock:
            if data.get("ports"):
                data["forward_status"] = {**data.get("forward_status", {}), "state": "disconnected",
                                          "checked_at": time.time()}
                for port in data["ports"]:
                    port["status"] = "disconnected"
        server.close()
        if controller:
            controller.join(timeout=3)
        cleaned = False
        if data["status"] == "reaping":
            try:
                with state.lock(path):
                    kube.destroy(data["identity"])
                    data["status"] = "reaped"
                    state.cleanup(path, data)
                    cleaned = True
            except PodgroveError as exc:
                data.update({"status": "error", "error": str(exc)})
                exit_code = 1
        if not cleaned:
            persist()
            if bound:
                Path(data["socket"]).unlink(missing_ok=True)
    return exit_code
