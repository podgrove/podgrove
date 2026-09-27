#!/usr/bin/env python3
"""Observe one existing, explicitly scoped fixture using an immutable installed CLI.

No setup or cleanup commands are issued. Status reads extend the fixture's TTL.
Optional fault injection terminates one proven supervisor-owned port-forward.
A run shorter than four hours is only a smoke test, never long-soak evidence.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
import io
from pathlib import Path
import re
import selectors
import shlex
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import zipfile

MAX_BYTES = 1024 * 1024
HTTP_TIMEOUT = 5.0
MANAGED = "app.kubernetes.io/managed-by"
ENVIRONMENT = "podgrove.dev/environment"


class Refused(RuntimeError):
    """Safe diagnostic that never includes subprocess output or private state."""


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def private_read(path: Path, *, limit=MAX_BYTES, allow_hardlinks=False) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or (not allow_hardlinks and info.st_nlink != 1)
                or info.st_mode & 0o022 or info.st_size > limit):
            raise Refused("Expected a bounded, owned regular file without shared write access")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise Refused("File exceeded the read limit")
        return value
    finally:
        os.close(fd)


def stop_process(process):
    # The leader may have exited while an authentication helper still owns
    # stdout. Reap the process group created by this command in either case.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)


def read_command(command, env, stop, *, timeout=40, limit=MAX_BYTES):
    if stop.is_set():
        raise Refused("Observation cancelled")
    process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    deadline, output = time.monotonic() + timeout, bytearray()
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(process.stdout, selectors.EVENT_READ)
            while poll.get_map() or process.poll() is None:
                if stop.is_set():
                    raise Refused("Observation cancelled")
                if time.monotonic() >= deadline:
                    raise Refused("Observation command timed out")
                if not poll.get_map():
                    stop.wait(.05)
                    continue
                for key, _ in poll.select(.05):
                    chunk = os.read(key.fd, min(16384, limit + 1 - len(output)))
                    if not chunk:
                        poll.unregister(key.fileobj)
                    output.extend(chunk)
                    if len(output) > limit:
                        raise Refused("Observation command exceeded its output limit")
        return process.wait(), bytes(output)
    finally:
        stop_process(process)
        process.stdout.close()


BINARY_PROBE = r'''
import hashlib, importlib.metadata, json, pathlib, sys
import podgrove
prefix = pathlib.Path(sys.prefix).resolve()
module = pathlib.Path(podgrove.__file__).resolve()
assert module.is_relative_to(prefix), 'package is outside the selected venv'
dist = importlib.metadata.distribution('podgrove')
direct = json.loads(dist.read_text('direct_url.json') or '{}')
assert not direct.get('dir_info', {}).get('editable', False), 'editable installation'
assert dist.version == podgrove.__version__, 'version metadata differs'
files = sorted(p for p in module.parent.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
assert 0 < len(files) <= 10000 and not any(p.is_symlink() for p in files), 'unexpected installed payload'
assert sum(p.stat().st_size for p in files) <= 32 * 1024 * 1024, 'installed package exceeds proof limit'
hashes = [(p.relative_to(module.parent).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()) for p in files]
print(json.dumps({'version': dist.version, 'prefix': str(prefix), 'python': sys.executable,
                  'package_sha256': hashlib.sha256(json.dumps(hashes).encode()).hexdigest(),
                  'package_files': len(files), 'editable': False}))
'''


ENTRYPOINT = """import sys
from podgrove.cli import main
if __name__ == "__main__":
    if sys.argv[0].endswith("-script.pyw"):
        sys.argv[0] = sys.argv[0][:-11]
    elif sys.argv[0].endswith(".exe"):
        sys.argv[0] = sys.argv[0][:-4]
    sys.exit(main())
"""


def verify_wheel_payload(venv, wheel, receipt):
    expected_name = f"podgrove-{receipt['version']}-py3-none-any.whl"
    raw = private_read(wheel, limit=64 * MAX_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    if wheel.name != expected_name or receipt.get("assets", {}).get(expected_name) != digest:
        raise Refused("Release wheel differs from the validated installation receipt")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = [item for item in archive.infolist() if item.filename.startswith("podgrove/") and not item.is_dir()]
        if (not 0 < len(entries) <= 10000 or sum(item.file_size for item in entries) > 32 * MAX_BYTES
                or len({item.filename for item in entries}) != len(entries)
                or any(".." in Path(item.filename).parts or "\\" in item.filename for item in entries)):
            raise Refused("Release wheel package payload is invalid or exceeds its proof limit")
        expected = {item.filename.removeprefix("podgrove/"): hashlib.sha256(archive.read(item)).hexdigest() for item in entries}
    candidates = list(venv.glob("lib/python*/site-packages/podgrove"))
    if len(candidates) != 1 or candidates[0].resolve() != candidates[0] or not candidates[0].is_dir():
        raise Refused("Installed package directory is missing, ambiguous or linked")
    package = candidates[0]
    entries = list(package.rglob("*"))
    if len(entries) > 20000 or any(item.is_symlink() for item in entries):
        raise Refused("Installed package contains unexpected linked files")
    files = [item for item in entries if item.is_file() and "__pycache__" not in item.parts]
    if {item.relative_to(package).as_posix() for item in files} != set(expected):
        raise Refused("Installed package file set differs from the validated release wheel")
    for item in files:
        if hashlib.sha256(private_read(item, limit=32 * MAX_BYTES, allow_hardlinks=True)).hexdigest() != expected[item.relative_to(package).as_posix()]:
            raise Refused("Installed package bytes differ from the validated release wheel")
    return digest


def binary_proof(binary, env, stop, *, wheel):
    executable = binary.resolve(strict=True)
    venv = executable.parent.parent
    if executable.name != "podgrove" or executable.parent.name != "bin" or venv.name != "venv":
        raise Refused("Select the absolute podgrove executable in a versioned installed venv")
    script = private_read(executable, limit=65536)
    python = venv / "bin/python"
    if (not script.startswith(("#!" + str(python) + "\n").encode())
            or ast.dump(ast.parse(script.decode())) != ast.dump(ast.parse(ENTRYPOINT))):
        raise Refused("Installed entry point does not bind to its own venv interpreter")
    receipt_raw = private_read(venv.parent / "installation.json")
    receipt = json.loads(receipt_raw)
    commit, version = receipt.get("source_commit", ""), receipt.get("version", "")
    if (not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            or receipt.get("tag") != "v" + version or venv.parent.name != f"{version}-{commit[:12]}"
            or receipt.get("executable") != str(executable)):
        raise Refused("Installed release receipt does not bind version, commit and executable")
    wheel_sha256 = verify_wheel_payload(venv, wheel, receipt)
    # Avoid existing bytecode caches for observations, without changing the
    # installed venv. The CLI receives the same setting through its environment.
    cache = env.get("PYTHONPYCACHEPREFIX", str(venv.parent / ".unused-soak-cache"))
    code, raw = read_command([str(python), "-I", "-B", "-X", f"pycache_prefix={cache}", "-c", BINARY_PROBE], env, stop, timeout=15)
    if code:
        raise Refused("Installed non-editable package verification failed")
    proof = json.loads(raw)
    if proof.get("version") != version or proof.get("prefix") != str(venv) or proof.get("editable") is not False:
        raise Refused("Installed package differs from its release receipt")
    return {**proof, "binary": str(executable), "source_commit": commit, "tag": receipt["tag"],
            "wheel_sha256": wheel_sha256, "binary_sha256": hashlib.sha256(script).hexdigest(), "receipt_sha256": hashlib.sha256(receipt_raw).hexdigest()}


def read_state(args):
    path = args.state_home / f"{args.identity}-{hashlib.sha256(args.context.encode()).hexdigest()[:8]}.json"
    data = json.loads(private_read(path))
    root = Path(data.get("root", ""))
    selected = Path(data.get("config_root", str(root)))
    if (not root.is_absolute() or root != root.resolve() or not selected.is_relative_to(root)
            or args.project_directory not in (root, selected) or selected != selected.resolve()
            or hashlib.sha256(str(root).encode()).hexdigest()[:12] != args.identity
            or data.get("identity") != args.identity or data.get("context") != args.context
            or data.get("namespace") != args.namespace or type(data.get("pid")) is not int or data["pid"] <= 1):
        raise Refused("Existing state does not match the explicit worktree, context, namespace and identity")
    ports = data.get("ports")
    if not isinstance(ports, list) or not ports or any(
            not isinstance(p, dict) or any(type(p.get(k)) is not int or not 1 <= p[k] <= 65535
                                          for k in ("local", "published", "target")) for p in ports):
        raise Refused("State has no valid existing application forwards")
    if args.endpoint.port not in {p["local"] for p in ports}:
        raise Refused("Probe URL is not a recorded application endpoint")
    return data, path


def state_binding(data):
    return {**{key: data[key] for key in ("pid", "identity", "root", "namespace", "context")},
            "ports": [{key: port.get(key) for key in ("service", "target", "published", "local")}
                      for port in data["ports"]]}


def owned_uids(args, env, stop):
    resources = []
    for kind, name in (("statefulset", "pg-" + args.identity), ("pod", "pg-" + args.identity + "-0"),
                       ("persistentvolumeclaim", "pg-" + args.identity)):
        code, raw = read_command(["kubectl", "--context", args.context, "--namespace", args.namespace,
                                  "--request-timeout=30s", "get", kind, name, "-o", "json"], env, stop)
        if code:
            raise Refused("Cannot read the exact namespaced fixture resource")
        data = json.loads(raw)
        meta = data.get("metadata", {})
        if (meta.get("name") != name or meta.get("namespace") != args.namespace or meta.get("deletionTimestamp")
                or not isinstance(meta.get("uid"), str) or not meta["uid"]
                or meta.get("labels", {}).get(MANAGED) != "podgrove"
                or meta.get("labels", {}).get(ENVIRONMENT) != args.identity):
            raise Refused("Fixture resource is missing, deleting or foreign")
        resources.append(data)
    sts, pod, _ = resources
    owners = [owner for owner in pod.get("metadata", {}).get("ownerReferences", []) if owner.get("controller") is True]
    claims = [v["persistentVolumeClaim"].get("claimName") for v in pod.get("spec", {}).get("volumes", [])
              if "persistentVolumeClaim" in v]
    if (len(owners) != 1 or owners[0].get("kind") != "StatefulSet" or owners[0].get("apiVersion") != "apps/v1"
            or owners[0].get("name") != sts["metadata"]["name"] or owners[0].get("uid") != sts["metadata"]["uid"]
            or claims != ["pg-" + args.identity]):
        raise Refused("Fixture Pod is not bound to its owned controller and PVC")
    return {kind: item["metadata"]["uid"] for kind, item in zip(("statefulset", "pod", "pvc"), resources)}


def http_probe(endpoint, marker):
    connection = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=HTTP_TIMEOUT)
    deadline, timer = time.monotonic() + HTTP_TIMEOUT, None
    expired = threading.Event()
    try:
        connection.connect()
        active = connection.sock
        def expire():
            expired.set()
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(max(.001, deadline - time.monotonic()), expire)
        timer.start()
        connection.request("GET", endpoint.path or "/", headers={"Connection": "close"})
        response = connection.getresponse()
        raw = response.read(65537)
        if expired.is_set() or time.monotonic() >= deadline or response.status != 200 or len(raw) > 65536:
            return False
        value = json.loads(raw)
        return isinstance(value, dict) and value.get("ok") is True and value.get("marker") == marker
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        if timer is not None:
            timer.cancel()
            timer.join(timeout=1)
        connection.close()


def process_table(env, stop):
    code, raw = read_command(["ps", "-axo", "pid=,ppid=,uid=,lstart=,command="], env, stop, timeout=10, limit=8 * MAX_BYTES)
    if code:
        raise Refused("Cannot verify process ownership")
    table = {}
    for line in raw.decode(errors="replace").splitlines():
        parts = line.split(None, 8)
        if len(parts) != 9 or not all(value.isdigit() for value in parts[:3]):
            continue
        try:
            argv = shlex.split(parts[8])
        except ValueError:
            continue
        pid, parent, uid = map(int, parts[:3])
        table[pid] = {"pid": pid, "parent": parent, "uid": uid, "started": " ".join(parts[3:8]), "argv": argv}
    return table


def forward_process(table, args, data, path, proof):
    supervisor = table.get(data["pid"])
    expected = [proof["python"], "-m", "podgrove", "_serve", str(path)]
    if not supervisor or supervisor["uid"] != os.getuid() or supervisor["argv"] != expected:
        raise Refused("Cannot prove the saved supervisor uses the selected installed runtime")
    arguments = ["--context", args.context, "--namespace", args.namespace, "--request-timeout=30s", "port-forward",
                 f"pod/pg-{args.identity}-0", "--address=127.0.0.1", "--request-timeout=0",
                 *(f"{p['local']}:{p['published']}" for p in data["ports"])]
    matches = []
    for process in table.values():
        argv = process["argv"]
        if (process["uid"] != os.getuid() or not argv or Path(argv[0]).name != "kubectl" or argv[1:] != arguments):
            continue
        parent = process["parent"]
        for _ in range(8):
            if parent == supervisor["pid"]:
                matches.append(process)
                break
            ancestor = table.get(parent)
            if not ancestor or ancestor["uid"] != os.getuid():
                break
            parent = ancestor["parent"]
    if len(matches) != 1:
        raise Refused("Cannot prove exactly one matching supervisor-owned application forward")
    return {"pid": matches[0]["pid"], "parent": matches[0]["parent"], "started": matches[0]["started"],
            "supervisor_pid": supervisor["pid"], "supervisor_started": supervisor["started"]}


def sample_status(args, env, stop):
    code, raw = read_command([str(args.binary), "status", "--project-directory", str(args.project_directory),
                             "--context", args.context, "--namespace", args.namespace, "--json"], env, stop)
    result = json.loads(raw)
    if any(result.get(key) != getattr(args, key) for key in ("identity", "context", "namespace")):
        raise Refused("Status returned a different environment binding")
    # Never persist raw status/state/errors; those can contain application output.
    safe = {"exit_code": code, "status": result.get("status"),
            "forward_state": result.get("forward_status", {}).get("state"),
            "health_state": result.get("health_status", {}).get("state")}
    return code == 0 and safe["status"] == "ready" and safe["forward_state"] == "ready", safe


def run(args, stop):
    args.output.mkdir(mode=0o700)  # Exclusive: prior evidence is never reused.
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "PODGROVE", "DOCKER"))}
    env.update(KUBECONFIG=str(args.kubeconfig), PODGROVE_STATE_HOME=str(args.state_home), PYTHONDONTWRITEBYTECODE="1",
               PYTHONPYCACHEPREFIX=str(args.output / ".unused-bytecode-cache"))
    started, started_utc = time.monotonic(), utc()
    counts = {"http": 0, "status": 0, "errors": 0, "recovery_samples": 0}
    summary = {"started_utc": started_utc, "started_monotonic": started, "requested_seconds": args.duration,
               "context": args.context, "namespace": args.namespace, "identity": args.identity,
               "project_directory": str(args.project_directory), "passed": False,
               "four_hour_proof": False, "fault": {"status": "not_requested"}}
    log_path = args.output / "samples.jsonl"
    fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as log:
        def record(kind, **values):
            row = {"utc": utc(), "elapsed_seconds": round(time.monotonic() - started, 3), "kind": kind, **values}
            log.write(json.dumps(row, sort_keys=True) + "\n")
            log.flush()
        try:
            proof = binary_proof(args.binary, env, stop, wheel=args.wheel)
            args.binary = Path(proof["binary"])  # Freeze a stable symlink before any status invocation.
            data, path = read_state(args)
            binding = state_binding(data)
            uids = owned_uids(args, env, stop)
            marker = private_read(args.marker_file, limit=4096).decode().strip()
            if not marker:
                raise Refused("Fixture marker must not be empty")
            summary.update(binary=proof, resource_uids=uids, supervisor_pid=data["pid"],
                           marker_sha256=hashlib.sha256(marker.encode()).hexdigest())
            # Binding proof is mandatory even when no signal will be sent.
            selected = forward_process(process_table(env, stop), args, data, path, proof)
            summary["initial_forward"] = selected
            summary["preflight_started_utc"] = started_utc
            summary["preflight_seconds"] = time.monotonic() - started
            started, started_utc = time.monotonic(), utc()
            summary.update(started_utc=started_utc, started_monotonic=started)
            record("start", version=proof["version"], source_commit=proof["source_commit"], resource_uids=uids)
            next_http = next_status = time.monotonic()
            last_http = last_status = started
            last_http_wall = time.time()
            max_http_gap = max_status_gap = max_http_wall_gap = 0.0
            deadline = started + args.duration
            fault_deadline = None
            recovery_http = recovery_status = False
            while not stop.is_set() and time.monotonic() < deadline:
                now = time.monotonic()
                if args.inject_after is not None and summary["fault"]["status"] == "not_requested" and now - started >= args.inject_after:
                    try:
                        if owned_uids(args, env, stop) != uids:
                            raise Refused("Resource UID changed before fault injection")
                        first = forward_process(process_table(env, stop), args, data, path, proof)
                        second = forward_process(process_table(env, stop), args, data, path, proof)
                        if first != second:
                            raise Refused("Forward process changed before fault injection")
                        os.kill(first["pid"], signal.SIGTERM)  # One exact child PID only; never a group or supervisor.
                        summary["fault"] = {"status": "injected", **first, "utc": utc(), "elapsed_seconds": now - started}
                        fault_deadline = time.monotonic() + args.recovery_timeout
                        recovery_http = recovery_status = False
                        next_http = next_status = time.monotonic()
                        record("fault", **summary["fault"])
                    except (Refused, OSError):
                        summary["fault"] = {"status": "skipped", "reason": "Could not prove unique unchanged child ownership"}
                        record("fault", **summary["fault"])
                if now >= next_status:
                    current, _ = read_state(args)
                    if state_binding(current) != binding or owned_uids(args, env, stop) != uids:
                        raise Refused("Recorded fixture binding or resource UID changed during the soak")
                    if binary_proof(args.binary, env, stop, wheel=args.wheel) != proof:
                        raise Refused("Installed runtime changed during the soak")
                    ok, values = sample_status(args, env, stop)
                    gap = time.monotonic() - last_status
                    last_status = time.monotonic()
                    max_status_gap = max(max_status_gap, gap)
                    counts["status"] += 1
                    recovering = fault_deadline is not None and time.monotonic() <= fault_deadline
                    counts["recovery_samples" if recovering else "errors"] += int(not ok)
                    record("status", ok=ok, recovering=recovering, runtime_verified=True, ownership_verified=True, **values)
                    recovery_status = ok
                    next_status = time.monotonic() + args.status_interval
                if now >= next_http:
                    ok = http_probe(args.endpoint, marker)
                    gap = time.monotonic() - last_http
                    last_http = time.monotonic()
                    max_http_wall_gap = max(max_http_wall_gap, max(0, time.time() - last_http_wall))
                    last_http_wall = time.time()
                    max_http_gap = max(max_http_gap, gap)
                    counts["http"] += 1
                    recovering = fault_deadline is not None and time.monotonic() <= fault_deadline
                    counts["recovery_samples" if recovering else "errors"] += int(not ok)
                    record("http", ok=ok, recovering=recovering)
                    recovery_http = ok
                    next_http = time.monotonic() + args.probe_interval
                if fault_deadline is not None:
                    if recovery_http and recovery_status:
                        try:
                            replacement = forward_process(process_table(env, stop), args, data, path, proof)
                            if replacement["pid"] != summary["fault"]["pid"]:
                                summary["fault"].update(status="recovered", replacement=replacement,
                                                        recovery_seconds=time.monotonic() - started - summary["fault"]["elapsed_seconds"])
                                record("recovery", **summary["fault"])
                                fault_deadline = None
                        except Refused:
                            pass
                    if fault_deadline is not None and time.monotonic() > fault_deadline:
                        summary["fault"]["status"] = "failed"
                        counts["errors"] += 1
                        record("recovery", ok=False, reason="Recovery deadline exceeded")
                        fault_deadline = None
                stop.wait(min(.25, max(0, min(next_http, next_status, deadline) - time.monotonic())))
            if not stop.is_set():
                if binary_proof(args.binary, env, stop, wheel=args.wheel) != proof or owned_uids(args, env, stop) != uids:
                    raise Refused("Final runtime or resource identity changed")
                max_http_gap = max(max_http_gap, time.monotonic() - last_http)
                max_status_gap = max(max_status_gap, time.monotonic() - last_status)
                max_http_wall_gap = max(max_http_wall_gap, max(0, time.time() - last_http_wall))
                summary.update(max_http_gap_seconds=max_http_gap, max_http_wall_gap_seconds=max_http_wall_gap,
                               max_status_gap_seconds=max_status_gap)
                if max(max_http_gap, max_http_wall_gap) > max(30, args.probe_interval * 3) or max_status_gap > max(90, args.status_interval * 3):
                    counts["errors"] += 1
                    record("sampling_gap", ok=False, http_seconds=max_http_gap, http_wall_seconds=max_http_wall_gap, status_seconds=max_status_gap)
                summary["passed"] = (counts["errors"] == 0 and counts["http"] > 0 and counts["status"] > 0
                                     and (args.inject_after is None or summary["fault"]["status"] == "recovered"))
            else:
                summary["cancelled"] = True
        except Exception as error:
            counts["errors"] += 1
            # Only our controlled messages are safe. Foreign exception payloads
            # may include command output, tokens or application content.
            record("fatal", error=str(error) if isinstance(error, Refused) else type(error).__name__)
        finally:
            if stop.is_set():
                summary["cancelled"] = True
                summary["passed"] = False
            elapsed = time.monotonic() - started
            summary.update(ended_utc=utc(), ended_monotonic=time.monotonic(), elapsed_seconds=elapsed, counts=counts,
                           four_hour_proof=summary["passed"] and elapsed >= 14400 and args.duration >= 14400)
            record("end", passed=summary["passed"], four_hour_proof=summary["four_hour_proof"], counts=counts)
            fd = os.open(args.output / "result.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as result:
                json.dump(summary, result, indent=2, sort_keys=True)
                result.write("\n")
    print(json.dumps({"output": str(args.output), "passed": summary["passed"], "four_hour_proof": summary["four_hour_proof"]}))
    return 0 if summary["passed"] else 1


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    for name in ("binary", "wheel", "project-directory", "state-home", "kubeconfig", "marker-file", "output"):
        result.add_argument("--" + name, type=Path, required=True)
    for name in ("context", "namespace", "identity", "url"):
        result.add_argument("--" + name, required=True)
    result.add_argument("--duration", type=float, default=14460)
    result.add_argument("--probe-interval", type=float, default=10)
    result.add_argument("--status-interval", type=float, default=60)
    result.add_argument("--inject-after", type=float, help="Terminate one proven own forward after this many seconds")
    result.add_argument("--recovery-timeout", type=float, default=90)
    return result


def arguments(argv=None):
    p = parser()
    args = p.parse_args(argv)
    for name in ("binary", "wheel", "project_directory", "state_home", "kubeconfig", "marker_file", "output"):
        path = getattr(args, name)
        if not path.is_absolute() or ".." in path.parts:
            p.error(f"--{name.replace('_', '-')} must be an explicit absolute path")
        if name != "binary" and path != path.resolve():
            p.error(f"--{name.replace('_', '-')} cannot traverse symlinks")
    if (not args.context.strip() or any(c in args.context for c in "\r\n\0")
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", args.namespace)
            or not re.fullmatch(r"[a-f0-9]{12}", args.identity)):
        p.error("Expected exact context, Kubernetes namespace and 12-character environment identity")
    if (not 0 < args.duration <= 7 * 86400 or not .01 <= args.probe_interval <= 60
            or not .01 <= args.status_interval <= 300 or not 1 <= args.recovery_timeout <= 300
            or args.inject_after is not None and not 0 <= args.inject_after < args.duration):
        p.error("Invalid duration, sampling interval or fault timing")
    args.endpoint = urlsplit(args.url)
    try:
        valid_url = (args.endpoint.scheme == "http" and args.endpoint.hostname == "127.0.0.1"
                     and args.endpoint.port is not None and 1 <= args.endpoint.port <= 65535 and not args.endpoint.username and not args.endpoint.password
                     and not args.endpoint.query and not args.endpoint.fragment)
    except ValueError:
        valid_url = False
    if not valid_url or not args.marker_file.is_relative_to(args.project_directory):
        p.error("Use a loopback HTTP fixture URL and a marker file inside the selected project")
    private_read(args.kubeconfig)
    return args


def main():
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        return run(arguments(), stop)
    except (OSError, Refused) as error:
        print(f"Soak refused: {str(error) if isinstance(error, Refused) else type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
