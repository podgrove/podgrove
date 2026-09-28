#!/usr/bin/env python3
"""Explicit live acceptance for reverse TCP and declared same-namespace links."""
from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

MANAGED = "app.kubernetes.io/managed-by"
ENVIRONMENT = "podgrove.dev/environment"
KINDS = "statefulset,pod,pvc,configmap,networkpolicy,service,poddisruptionbudget"
ALLOWED_KINDS = {"StatefulSet", "Pod", "PersistentVolumeClaim", "ConfigMap", "NetworkPolicy", "Service", "PodDisruptionBudget"}
IMAGE = "python:3.12-alpine"


class AcceptanceError(Exception):
    """Preserve evidence and attempt cleanup of every started private fixture."""


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def identity(path):
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]


def checked_objects(payload, namespace, ident, expected=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise AcceptanceError("Namespaced resource inventory was not a Kubernetes list")
    result = {}
    for item in payload["items"]:
        meta = item.get("metadata", {})
        name, uid, kind = meta.get("name"), meta.get("uid"), item.get("kind")
        labels = meta.get("labels", {})
        pattern = rf"pg-{ident}(?:-0|-link-[0-9a-f]{{10}}(?:-in|-out)?)?"
        if (kind not in ALLOWED_KINDS or not isinstance(name, str) or not re.fullmatch(pattern, name)
                or meta.get("namespace") != namespace or labels.get(MANAGED) != "podgrove"
                or labels.get(ENVIRONMENT) != ident or not isinstance(uid, str) or not uid):
            raise AcceptanceError("Fixture resource ownership or name is unexpected; refusing cleanup")
        key = kind + "/" + name
        if key in result or (expected and key in expected and expected[key] != uid):
            raise AcceptanceError("Fixture resource identity changed; refusing cleanup")
        result[key] = uid
    return result


def probe_program(host, port, *, expected=None, path="/"):
    if expected is None:
        return (
            "import json,socket,sys\n"
            "try:\n"
            f" s=socket.create_connection(({host!r},{port}),timeout=3);s.close()\n"
            "except TimeoutError:\n print(json.dumps({'blocked':True,'reason':'TCP timeout'}));sys.exit(0)\n"
            "except OSError as e:\n print(json.dumps({'blocked':False,'reason':type(e).__name__}));sys.exit(2)\n"
            "print(json.dumps({'blocked':False,'reason':'connected'}));sys.exit(1)\n"
        )
    url_host = "[" + host + "]" if ":" in host else host
    return (
        "import hashlib,json,urllib.request\n"
        "opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
        f"response=opener.open({'http://' + url_host + ':' + str(port) + path!r},timeout=10)\n"
        "body=response.read(1048576)\n"
        f"assert response.status==200 and body=={expected!r},'unexpected endpoint response'\n"
        "print(json.dumps({'matched':True,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}))\n"
    )


def fixture_config(context, namespace, storage_class):
    return {"version": 1, "cluster": {"context": context, "namespace": namespace,
            "namespace_mode": "shared", "storage_class": storage_class}, "node_mode": "shared",
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                          "limits": {"cpu": "1", "memory": "1Gi"}},
            "init_resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                               "limits": {"cpu": "100m", "memory": "32Mi"}},
            "storage": {"size": "2Gi"}, "ttl": "1h", "compose": {"files": ["compose.yml"]}}


def fixture_compose(role, body):
    if role != "target":
        return {"services": {"client": {"image": IMAGE, "command": ["python", "-u", "-c", "import time;time.sleep(3600)"]}}}
    program = (
        "import http.server,threading,time\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        f"  data={body!r};self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)\n"
        " def log_message(self,*args):pass\n"
        "for port in (8080,8081):\n"
        " server=http.server.ThreadingHTTPServer(('0.0.0.0',port),Handler)\n"
        " threading.Thread(target=server.serve_forever,daemon=True).start()\n"
        "time.sleep(3600)\n"
    )
    return {"services": {"gateway": {"image": IMAGE, "command": ["python", "-u", "-c", program],
            "ports": ["8080", "8081"], "healthcheck": {"test": ["CMD", "python", "-c",
                "import socket;[socket.create_connection(('127.0.0.1',p),2).close() for p in (8080,8081)]"],
                "interval": "1s", "timeout": "3s", "retries": 60}}}}


class LoopbackServer:
    def __init__(self, body, nonce):
        self.body, self.path = body, "/" + nonce
        expected_path, response_body = self.path, body
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                payload = response_body if self.path == expected_path else b"not found"
                self.send_response(200 if self.path == expected_path else 404)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *_args):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class Runner:
    def __init__(self, args):
        self.args = args
        self.output = args.output
        self.fixtures = []
        self.counter = 0
        self.commands = []
        self.base = None
        self.local = None
        self.output_created = False
        self.result = {"passed": False, "context": args.context, "namespace": args.namespace,
                       "started_at": time.time(), "checks": {}, "cleanup": {}, "commands": self.commands}

    def command(self, argv, *, role=None, timeout=60, check=True):
        self.counter += 1
        stem = f"{self.counter:03d}-{role['role'] if role else 'preflight'}"
        stdout, stderr = self.output / (stem + ".stdout"), self.output / (stem + ".stderr")
        env = os.environ.copy()
        if role:
            env["PODGROVE_STATE_HOME"] = str(role["state"])
        record = {"argv": list(argv), "stdout": stdout.name, "stderr": stderr.name, "started_at": time.time()}
        self.commands.append(record)
        with stdout.open("xb") as out, stderr.open("xb") as err:
            process = subprocess.Popen(argv, cwd=role["root"] if role else self.base,
                                       env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
            try:
                process.wait(timeout=timeout)
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        if process.poll() is None:
                            raise
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        except PermissionError:
                            if process.poll() is None:
                                raise
                        process.wait(timeout=3)
                record.update(returncode=process.returncode, finished_at=time.time())
        if stdout.stat().st_size > 2 * 1024 * 1024:
            raise AcceptanceError("Command response exceeded the evidence reader limit")
        value = stdout.read_text(encoding="utf-8")
        if check and process.returncode:
            raise AcceptanceError(f"Command failed; inspect {stem}.stdout/.stderr")
        return value, process.returncode

    def cli(self, role, command, *arguments, timeout=120, check=True):
        argv = [str(self.args.podgrove_bin), command, "--project-directory", str(role["root"]),
                "--context", self.args.context, "--namespace", self.args.namespace, *arguments]
        return self.command(argv, role=role, timeout=timeout, check=check)

    def inventory(self, role):
        selector = f"{MANAGED}=podgrove,{ENVIRONMENT}={role['identity']}"
        raw, _ = self.command(["kubectl", "--context", self.args.context, "--namespace", self.args.namespace,
                               "--request-timeout=20s", "get", KINDS, "-l", selector, "-o", "json"], timeout=30)
        payload = json.loads(raw)
        proof = checked_objects(payload, self.args.namespace, role["identity"], role.get("captured"))
        return payload, proof

    def verify_exact_absence(self, role, captured):
        names = {(kind, "pg-" + role["identity"] + ("-0" if kind == "Pod" else "")) for kind in ALLOWED_KINDS}
        names.update(tuple(key.split("/", 1)) for key in captured)
        for kind, name in sorted(names):
            raw, _ = self.command(["kubectl", "--context", self.args.context, "--namespace", self.args.namespace,
                                   "--request-timeout=20s", "get", kind, name, "--ignore-not-found", "-o", "json"], timeout=30)
            if raw.strip():
                raise AcceptanceError("An exact fixture resource name remains after cleanup")

    def status(self, role):
        raw, _ = self.cli(role, "status", "--json", timeout=60)
        value = json.loads(raw)
        if (value.get("identity") != role["identity"] or value.get("context") != self.args.context
                or value.get("namespace") != self.args.namespace or value.get("status") != "ready"):
            raise AcceptanceError("Fixture status is not ready or has an unexpected binding")
        return value

    def start(self, role, *, refresh=False):
        role["attempted"] = True
        self.cli(role, "up", "--json", "--timeout", "600", *(["--refresh"] if refresh else []), timeout=660)
        value = self.status(role)
        pid = value.get("pid")
        if type(pid) is not int or pid <= 1:
            raise AcceptanceError("Ready fixture has no valid supervisor PID")
        process, code = self.command(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="], check=False)
        state_file = role["state"] / (role["identity"] + "-" + hashlib.sha256(self.args.context.encode()).hexdigest()[:8] + ".json")
        if code or not process.strip().endswith(" -I -B -m podgrove _serve " + str(state_file)):
            raise AcceptanceError("Supervisor process does not match this fixture's exact private state")
        role.setdefault("processes", []).append({"pid": pid, "description": process.strip()})
        payload, proof = self.inventory(role)
        role.setdefault("captured", {}).update(proof)
        write_json(self.output / (role["role"] + "-ready-resources.json"), payload)
        return value, payload

    def probe(self, name, role, host, port, *, expected=None, path="/"):
        raw, _ = self.cli(role, "exec", "client", "--", "python", "-c",
                          probe_program(host, port, expected=expected, path=path), timeout=45)
        value = json.loads(raw)
        required = "blocked" if expected is None else "matched"
        if value.get(required) is not True:
            raise AcceptanceError("Probe did not confirm " + name)
        self.result["checks"][name] = value

    def prepare(self):
        self.output.mkdir(mode=0o700)
        self.output_created = True
        self.base = Path(tempfile.mkdtemp(prefix="podgrove-connectivity-"))
        if any((parent / ".git").exists() for parent in (self.base, *self.base.parents)):
            raise AcceptanceError("Fixture temp parent is inside a Git worktree")
        nonce = uuid.uuid4().hex
        self.result.update(fixture_base=str(self.base), nonce=nonce, binary=str(self.args.podgrove_bin),
                           binary_sha256=hashlib.sha256(self.args.podgrove_bin.read_bytes()).hexdigest(),
                           inherited_kubeconfig=os.environ.get("KUBECONFIG"),
                           proof_scope="same namespace; no Namespace, Node, storage backend or cross-namespace proof")
        version, _ = self.command([str(self.args.podgrove_bin), "--version"])
        self.result["version"] = version.strip()
        self.target_body = ("target:" + nonce).encode()
        self.local_body = ("local:" + nonce).encode()
        self.local = LoopbackServer(self.local_body, nonce)
        for name in ("target", "source", "third"):
            root, state = self.base / name, self.base / (name + "-state")
            root.mkdir(mode=0o700)
            state.mkdir(mode=0o700)
            role = {"role": name, "root": root, "state": state, "identity": identity(root), "attempted": False}
            role["config"] = fixture_config(self.args.context, self.args.namespace, self.args.storage_class)
            if name == "source":
                role["config"]["reverse"] = [{"local_port": self.local.port, "remote_port": 18081}]
            self.fixtures.append(role)
            write_json(root / "podgrove.yml", role["config"])
            write_json(root / "compose.yml", fixture_compose(name, self.target_body))
            _, proof = self.inventory(role)
            if proof:
                raise AcceptanceError("Fresh fixture identity already exists; refusing adoption")
            self.cli(role, "validate", "--json", timeout=60)
        self.result["fixtures"] = [{"role": role["role"], "root": str(role["root"]),
                                    "state": str(role["state"]), "identity": role["identity"]} for role in self.fixtures]

    def exercise(self):
        target, source, third = self.fixtures
        target_status, resources = self.start(target)
        ports = {port["target"]: port["published"] for port in target_status["ports"] if port["service"] == "gateway"}
        if set(ports) != {8080, 8081} or any(type(port) is not int or not 1 <= port <= 65535 for port in ports.values()):
            raise AcceptanceError("Target published-port inventory is incomplete")
        pods = [item for item in resources["items"] if item["kind"] == "Pod"]
        if len(pods) != 1:
            raise AcceptanceError("Target engine Pod inventory is ambiguous")
        target_ip = str(ipaddress.ip_address(pods[0]["status"]["podIP"]))
        self.start(source)
        self.probe("before_declaration_blocked", source, target_ip, ports[8080])
        self.probe("reverse_loopback", source, "host.docker.internal", 18081,
                   expected=self.local_body, path=self.local.path)
        source["config"]["connect"] = [{"name": "api", "environment": target["identity"], "service": "gateway", "port": 8080}]
        write_json(source["root"] / "podgrove.yml", source["config"])
        self.start(source, refresh=True)
        self.probe("declared_link", source, "api.podgrove", 8080, expected=self.target_body)
        self.probe("undeclared_target_port_blocked", source, target_ip, ports[8081])
        self.probe("reverse_after_refresh", source, "host.docker.internal", 18081,
                   expected=self.local_body, path=self.local.path)
        self.start(third)
        self.probe("third_environment_blocked", third, target_ip, ports[8080])
        self.result["checks"]["final_ready"] = {role["role"]: self.status(role)["status"] for role in self.fixtures}

    def cleanup(self):
        for role in reversed(self.fixtures):
            entry = {"attempted": role["attempted"], "passed": False}
            self.result["cleanup"][role["role"]] = entry
            try:
                try:
                    for filename in ("podgrove.yml", "compose.yml"):
                        shutil.copyfile(role["root"] / filename, self.output / (role["role"] + "-" + filename))
                    for log in role["state"].glob("*.log"):
                        shutil.copyfile(log, self.output / (role["role"] + "-session.log"))
                except Exception as exc:
                    entry["diagnostic_error"] = type(exc).__name__ + ": " + str(exc)
                if role["attempted"]:
                    try:
                        raw, _ = self.cli(role, "status", "--json", check=False)
                        (self.output / (role["role"] + "-before-down.json")).write_text(raw)
                    except Exception as exc:
                        entry["status_error"] = type(exc).__name__ + ": " + str(exc)
                    _, before = self.inventory(role)
                    entry["captured_before_down"] = before
                    raw, _ = self.cli(role, "down", "--json", timeout=300)
                    down = json.loads(raw)
                    if (down.get("identity") != role["identity"] or down.get("status") != "removed"
                            or down.get("namespace_retained") is not True or down.get("bootstrap_retained") is not True):
                        raise AcceptanceError("Down result did not confirm exact fixture cleanup")
                _, remaining = self.inventory(role)
                if remaining or list(role["state"].glob("*.json")):
                    raise AcceptanceError("Owned fixture resources or runtime state remain")
                self.verify_exact_absence(role, {**role.get("captured", {}), **entry.get("captured_before_down", {})})
                for process in role.get("processes", []):
                    current, code = self.command(["ps", "-p", str(process["pid"]), "-o", "lstart=", "-o", "command="], check=False)
                    if code not in (0, 1) or (code == 0 and current.strip() == process["description"]):
                        raise AcceptanceError("Captured fixture supervisor remains or its absence cannot be read")
                entry.update(passed=True, resources_absent=True, state_absent=True, supervisors_absent=True)
            except Exception as exc:
                entry["error"] = type(exc).__name__ + ": " + str(exc)
        if self.local:
            self.local.close()
        if self.base and self.fixtures and all(value.get("passed") for value in self.result["cleanup"].values()):
            shutil.rmtree(self.base)
            self.result["fixture_directories_removed"] = True

    def run(self):
        completed = False
        try:
            self.prepare()
            self.exercise()
            completed = True
        except (Exception, KeyboardInterrupt) as exc:
            self.result["error"] = type(exc).__name__ + ": " + str(exc)
        finally:
            try:
                self.cleanup()
            except (Exception, KeyboardInterrupt) as exc:
                self.result["cleanup_error"] = type(exc).__name__ + ": " + str(exc)
            self.result.update(finished_at=time.time(), passed=completed and bool(self.fixtures)
                               and not self.result.get("cleanup_error")
                               and all(value.get("passed") and not value.get("diagnostic_error")
                                       for value in self.result["cleanup"].values()))
            if self.output_created:
                write_json(self.output / "result.json", self.result)
        return 0 if self.result["passed"] else 1


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--podgrove-bin", type=Path, required=True)
    value.add_argument("--context", required=True)
    value.add_argument("--namespace", required=True)
    value.add_argument("--storage-class", required=True)
    value.add_argument("--output", type=Path, required=True, help="New private evidence directory; fixtures use fresh system temp directories")
    value.add_argument("--execute", action="store_true", help="Create and clean up three new engines in the explicit existing namespace")
    return value


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    if not args.execute:
        command.error("--execute is required; this acceptance creates three owned disposable environments")
    if not args.podgrove_bin.is_absolute() or not args.podgrove_bin.is_file() or not os.access(args.podgrove_bin, os.X_OK):
        command.error("--podgrove-bin must be an absolute executable path")
    args.podgrove_bin = args.podgrove_bin.resolve()
    if not args.context or any(ord(ch) < 32 for ch in args.context):
        command.error("--context must be explicit")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", args.namespace) or args.namespace.startswith("kube-"):
        command.error("--namespace must be an approved explicit non-system namespace")
    if (not args.output.is_absolute() or args.output.exists() or args.output.is_symlink()
            or not args.output.parent.is_dir()):
        command.error("--output must be a new absolute directory beneath an existing parent")
    if not args.storage_class or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", args.storage_class):
        command.error("--storage-class must be explicit")
    return Runner(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
