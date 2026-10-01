#!/usr/bin/env python3
"""Opt-in two-namespace proof of Podgrove engine network modes and cleanup."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

_SPEC = importlib.util.spec_from_file_location("podgrove_network_acceptance_base", Path(__file__).with_name("check_connectivity.py"))
base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(base)
AcceptanceError = base.AcceptanceError
MARKER = "podgrove-bootstrap"
PORTS = {"app": (8080, 18080), "private": (8081, 18081)}
AUTH_FAILURE = re.compile(r"unauthorized|credentials? (?:required|expired)|provide credentials|must be logged in|expiredtoken|unable to locate credentials", re.I)


def fixture_compose(body: bytes, *, target: bool) -> dict:
    services = base.fixture_compose("source", body)["services"]
    if target:
        for name, (port, published) in PORTS.items():
            program = (
                "import http.server\n"
                "class Handler(http.server.BaseHTTPRequestHandler):\n"
                " def do_GET(self):\n"
                f"  body={body!r};self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)\n"
                " def log_message(self,*args):pass\n"
                f"http.server.ThreadingHTTPServer(('0.0.0.0',{port}),Handler).serve_forever()\n"
            )
            services[name] = {"image": base.IMAGE, "command": ["python", "-u", "-c", program],
                              "ports": [{"target": port, "published": str(published), "host_ip": "0.0.0.0", "protocol": "tcp"}],
                              "healthcheck": {"test": ["CMD", "python", "-c", f"import socket;socket.create_connection(('127.0.0.1',{port}),2).close()"],
                                              "interval": "1s", "timeout": "3s", "retries": 60}}
    return {"services": services}


def network_config(mode: str, *, peer_namespace: str | None = None, target: bool = False,
                   worktree: str | None = None, ports: list[int] | None = None, expose_private: bool = False) -> dict:
    if mode not in ("disabled", "open", "selected"):
        raise AcceptanceError("Unexpected fixture mode")
    value = {"pod_to_pod": mode}
    if mode == "open" and peer_namespace:
        value["namespaces"] = [peer_namespace]
    if mode == "selected" and peer_namespace:
        if target:
            peer = {"namespace": peer_namespace}
            if worktree is not None:
                peer["worktree"] = worktree
            value["expose"] = [{"service": service, "from": [copy.deepcopy(peer)]}
                               for service in (["app", "private"] if expose_private else ["app"])]
        else:
            value["connect"] = [{"namespace": peer_namespace, "worktree": worktree or "apis-*", "ports": ports or [18080]}]
    return value


def marker_proof(value: dict, namespace: str) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("metadata"), dict):
        raise AcceptanceError("Expected the exact existing shared provisioning ConfigMap")
    metadata = value["metadata"]
    labels = metadata.get("labels", {})
    if not isinstance(labels, dict):
        raise AcceptanceError("Provisioning marker labels are malformed")
    if (value.get("kind") != "ConfigMap" or value.get("apiVersion") != "v1"
            or metadata.get("name") != MARKER or metadata.get("namespace") != namespace
            or not isinstance(metadata.get("uid"), str) or not metadata["uid"] or metadata.get("deletionTimestamp")
            or labels.get(base.MANAGED) != "podgrove" or labels.get("podgrove.dev/component") != "bootstrap"
            or base.ENVIRONMENT in labels or value.get("data") != {"version": "1", "namespace_mode": "shared"}):
        raise AcceptanceError("Both existing namespaces need an owned shared bootstrap marker; prepare access separately")
    return {"namespace": namespace, "uid": metadata["uid"], "data": value["data"], "labels": labels}


def publisher_proof(status: dict) -> dict:
    result = {}
    for service, (target, published) in PORTS.items():
        rows = [row for row in status.get("services", []) if row.get("Service") == service]
        if len(rows) != 1 or rows[0].get("State") != "running" or rows[0].get("Health") != "healthy":
            raise AcceptanceError("Both target HTTP services must be running and healthy")
        bindings = rows[0].get("Publishers")
        if not isinstance(bindings, list) or not bindings or any(
                not isinstance(item, dict) or item.get("URL") != "0.0.0.0"
                or item.get("TargetPort") != target or item.get("PublishedPort") != published
                or item.get("Protocol") != "tcp" for item in bindings):
            raise AcceptanceError("Target must publish its exact two wildcard TCP bindings")
        result[service] = {"target": target, "published": published, "host_ip": "0.0.0.0"}
    return result


def domain_from_search(search: list[str], namespace: str) -> str:
    if not isinstance(search, list) or any(not isinstance(token, str) for token in search):
        raise AcceptanceError("Resolver search list is malformed")
    found = set()
    for token in search:
        for prefix in (namespace + ".svc.", "svc."):
            if token.startswith(prefix):
                suffix = token[len(prefix):].rstrip(".")
                if valid_domain(suffix):
                    found.add(suffix)
    if len(found) != 1:
        raise AcceptanceError("Cannot infer one cluster DNS domain from the client resolver; supply --cluster-domain explicitly")
    return found.pop()


def valid_domain(value: str) -> bool:
    return isinstance(value, str) and len(value) <= 253 and bool(re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", value))


def probe_program(host: str, port: int, body: bytes | None) -> str:
    if body is None:
        return base.probe_program(host, port)
    return (
        "import hashlib,json,socket,sys,urllib.error,urllib.request\n"
        "try:\n"
        " opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
        f" response=opener.open({'http://' + host + ':' + str(port) + '/'!r},timeout=3)\n"
        " body=response.read(1048576)\n"
        f" assert response.status==200 and body=={body!r},'unexpected fixture bytes'\n"
        " print(json.dumps({'matched':True,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}))\n"
        "except Exception as error:\n"
        " cause=error.reason if isinstance(error,urllib.error.URLError) else error\n"
        " print(json.dumps({'matched':False,'reason':'TCP timeout' if isinstance(cause,TimeoutError) else type(cause).__name__}));sys.exit(2)\n"
    )


class NamespaceRunner(base.Runner):
    def __init__(self, args, auth_stop=None):
        super().__init__(args)
        self.auth_stop = auth_stop if auth_stop is not None else {}

    def command(self, argv, *, role=None, timeout=60, check=True):
        if self.auth_stop:
            raise AcceptanceError("Authentication failed earlier; all further external calls are stopped, including cleanup")
        count = len(self.commands)
        try:
            return super().command(argv, role=role, timeout=timeout, check=check)
        finally:
            for record in self.commands[count:]:
                for key in ("stdout", "stderr"):
                    path = self.output / record[key]
                    if not path.is_file():
                        continue
                    with path.open("r", encoding="utf-8", errors="replace") as stream:
                        overlap = ""
                        while chunk := stream.read(65536):
                            text = overlap + chunk
                            if AUTH_FAILURE.search(text):
                                self.auth_stop.update(namespace=self.args.namespace, identity=role.get("identity") if role else None,
                                                      reason="Authentication unavailable; ask the namespace owner before any further operation",
                                                      evidence=str(path), detected_at=time.time())
                                raise AcceptanceError("Authentication failed; stopped without trying alternate credentials or cleanup calls")
                            overlap = text[-256:]

    def cleanup_one(self, role):
        if role.get("cleanup_attempted"):
            return
        role["cleanup_attempted"] = True
        if not role["attempted"]:
            self.result["cleanup"][role["role"]] = {"attempted": False, "passed": True, "not_started": True}
            return
        if self.auth_stop:
            self.result["cleanup"][role["role"]] = {"attempted": False, "passed": False, "unconfirmed": True,
                                                      "identity": role["identity"], "namespace": role["namespace"],
                                                      "reason": "Authentication stop gate; owner action required"}
            return
        original, original_base = self.fixtures, self.base
        self.fixtures, self.base = [role], None
        try:
            super().cleanup()
        finally:
            self.fixtures, self.base = original, original_base
        role["cleanup_succeeded"] = self.result["cleanup"][role["role"]].get("passed") is True


class Runner:
    def __init__(self, args):
        self.args = args
        self.output = args.output
        self.created = False
        self.temp = None
        self.runners = {}
        self.roles = []
        self.auth_stop = {}
        self.same_only = getattr(args, "same_namespace_only", False)
        self.result = {"passed": False, "started_at": time.time(), "context": args.context,
                       "namespaces": [args.namespace_a] if self.same_only else [args.namespace_a, args.namespace_b],
                       "checks": {}, "cleanup": {}, "transitions": [], "same_namespace_only": self.same_only,
                       "scope": "At most two owned engines; existing prepared namespaces only; IPv4 TCP and engine DNS only; no Nodes or Namespace mutations"}
        if self.same_only:
            self.result["not_run"] = ["cross-namespace matrix", "wrong-namespace selection checks"]

    def prepare(self):
        self.output.mkdir(mode=0o700)
        self.created = True
        self.temp = Path(tempfile.mkdtemp(prefix="podgrove-pod-network-"))
        if any((parent / ".git").exists() for parent in (self.temp, *self.temp.parents)):
            raise AcceptanceError("Fresh fixture path must be outside Git")
        nonce = uuid.uuid4().hex[:16]
        self.body = ("podgrove-network:" + nonce + "\n").encode()
        self.result.update(binary=str(self.args.podgrove_bin), binary_sha256=hashlib.sha256(self.args.podgrove_bin.read_bytes()).hexdigest(),
                           fixture_root=str(self.temp), nonce=nonce)
        for key, namespace in (("a", self.args.namespace_a), ("b", self.args.namespace_b)):
            if self.same_only and key == "b":
                continue
            evidence = self.output / ("namespace-" + key)
            evidence.mkdir(mode=0o700)
            args = SimpleNamespace(**{**vars(self.args), "namespace": namespace, "output": evidence})
            runner = NamespaceRunner(args, self.auth_stop)
            runner.base = self.temp / key
            runner.base.mkdir(mode=0o700)
            self.runners[key] = runner
        for name, key, target in (("source", "a", False), ("same-namespace-target", "a", True), ("cross-namespace-target", "b", True)):
            if self.same_only and key == "b":
                self.result["cleanup"][name] = {"attempted": False, "passed": True, "not_started": True}
                continue
            runner = self.runners[key]
            root = runner.base / (("apis-network-" if target else "web-network-") + nonce)
            root.mkdir(mode=0o700)
            state = runner.output / (name + "-state")
            state.mkdir(mode=0o700)
            config = base.fixture_config(self.args.context, runner.args.namespace, self.args.storage_class)
            config.update(forward=[], network=network_config("open"))
            role = {"role": name, "root": root, "state": state, "identity": base.identity(root), "attempted": False,
                    "config": config, "runner": key, "namespace": runner.args.namespace, "target": target}
            runner.fixtures.append(role)
            self.roles.append(role)
            base.write_json(root / "podgrove.yml", config)
            base.write_json(root / "compose.yml", fixture_compose(self.body, target=target))
        self.result["fixtures"] = [{key: str(role[key]) for key in ("role", "root", "state", "identity", "namespace")} for role in self.roles]
        version, _ = self.runners["a"].command([str(self.args.podgrove_bin), "--version"])
        self.result["version"] = version.strip()
        self.result["bootstrap_before"] = self.bootstrap()
        for runner in self.runners.values():
            role = runner.fixtures[0]
            runner.cli(role, "doctor", "--json", timeout=120)
        for role in self.roles:
            runner = self.runners[role["runner"]]
            if runner.inventory(role)[1]:
                raise AcceptanceError("Fresh fixture identity already exists")
            runner.cli(role, "validate", "--json", timeout=60)

    def bootstrap(self):
        proofs = {}
        for key, runner in self.runners.items():
            raw, _ = runner.command(["kubectl", "--context", self.args.context, "--namespace", runner.args.namespace,
                                     "--request-timeout=20s", "get", "configmap", MARKER, "-o", "json"], timeout=30)
            proofs[key] = marker_proof(json.loads(raw), runner.args.namespace)
        return proofs

    def start(self, role):
        active = [item for item in self.roles if item["attempted"] and not item.get("cleanup_succeeded")]
        if not role["attempted"] and len(active) >= 2:
            raise AcceptanceError("Refusing a third engine before exact cleanup of an earlier target")
        runner = self.runners[role["runner"]]
        status, objects = runner.start(role, refresh=role["attempted"])
        if role["target"]:
            role["publishers"] = publisher_proof(status)
        return status, objects

    def mode(self, role, settings):
        transition = {"role": role["role"], "namespace": role["namespace"], "identity": role["identity"],
                      "settings": copy.deepcopy(settings), "started_at": time.time(), "passed": False}
        self.result["transitions"].append(transition)
        role["config"]["network"] = settings
        base.write_json(role["root"] / "podgrove.yml", role["config"])
        status = self.start(role)[0]
        transition.update(passed=True, finished_at=time.time(), status=status.get("pod_network_status"))
        return status

    def exec_json(self, role, program, *, timeout=45):
        raw, code = self.runners[role["runner"]].cli(role, "exec", "client", "--", "python", "-c", program,
                                                   timeout=timeout, check=False)
        try:
            result = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise AcceptanceError("Probe CLI did not return a JSON result; refusing to retry an authentication or transport error") from exc
        if not isinstance(result, dict):
            raise AcceptanceError("Probe output must be one JSON object")
        return result, code

    def endpoint(self, source, target, objects):
        pods = [item for item in objects["items"] if item["kind"] == "Pod"]
        if len(pods) != 1:
            raise AcceptanceError("Expected exactly one owned target engine Pod")
        pod = pods[0]
        controller = target["captured"].get("StatefulSet/pg-" + target["identity"])
        if (not controller or pod["metadata"].get("uid") != target["captured"].get("Pod/pg-" + target["identity"] + "-0")
                or not any(owner.get("uid") == controller and owner.get("kind") == "StatefulSet" and owner.get("controller") is True
                    for owner in pod["metadata"].get("ownerReferences", []))
                or pod["metadata"].get("deletionTimestamp")
                or pod.get("status", {}).get("phase") != "Running"
                or not any(item.get("type") == "Ready" and item.get("status") == "True" for item in pod["status"].get("conditions", []))):
            raise AcceptanceError("Target Pod lacks a Ready state and captured controller owner")
        address = ipaddress.ip_address(pod["status"]["podIP"])
        if address.version != 4:
            raise AcceptanceError("This fixture proves IPv4 TCP only")
        if self.args.cluster_domain:
            domain = self.args.cluster_domain
        else:
            resolver, code = self.exec_json(source, "import json;from pathlib import Path;print(json.dumps({'search':[s for l in Path('/etc/resolv.conf').read_text().splitlines() if l.startswith('search ') for s in l.split()[1:]]}))")
            if code:
                raise AcceptanceError("Could not read the owned Compose client's resolver search list")
            domain = domain_from_search(resolver.get("search", []), source["namespace"])
        host = f"pg-{target['identity']}-0.pg-{target['identity']}.{target['namespace']}.svc.{domain}"
        result, code = self.exec_json(source, f"import json,socket;print(json.dumps({{'addresses':sorted({{r[4][0] for r in socket.getaddrinfo({host!r},18080,socket.AF_INET,socket.SOCK_STREAM)}})}}))")
        if code or result.get("addresses") != [str(address)]:
            raise AcceptanceError("Engine DNS must resolve to the captured target Pod IP before any packet proof")
        return {"ip": str(address), "dns": host, "domain": domain, "pod_uid": pod["metadata"]["uid"], "publishers": target["publishers"]}

    def probe(self, name, source, host, port, *, allowed):
        started = time.monotonic()
        deadline = started + self.args.settle_timeout
        attempts = []
        while time.monotonic() < deadline:
            result, code = self.exec_json(source, probe_program(host, port, self.body if allowed else None),
                                          timeout=min(45, max(.1, deadline - time.monotonic())))
            attempts.append({"returncode": code, **result})
            matched = code == 0 and result.get("matched" if allowed else "blocked") is True
            if matched and (allowed or result.get("reason") == "TCP timeout") and time.monotonic() <= deadline:
                self.result["checks"][name] = {"host": host, "port": port, "expected": "allowed" if allowed else "blocked",
                                              "elapsed_seconds": time.monotonic() - started, "attempts": attempts}
                return
            pending = result.get("reason") == ("TCP timeout" if allowed else "connected")
            if not pending:
                raise AcceptanceError("Probe failed with a non-policy result; connection refusal, DNS errors and CLI failures are not isolation proof")
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        self.result["checks"][name] = {"passed": False, "attempts": attempts}
        raise AcceptanceError("Network mode did not converge within the fixed packet-probe budget")

    def pair(self, name, source, endpoint, *, allowed, port=18080):
        for address in ("ip", "dns"):
            self.probe(name + "-" + address, source, endpoint[address], port, allowed=allowed)

    @staticmethod
    def opened(role, other):
        return network_config("open", peer_namespace=other["namespace"] if other["namespace"] != role["namespace"] else None)

    def phase(self, name, source, target):
        self.mode(source, self.opened(source, target))
        target["config"]["network"] = self.opened(target, source)
        base.write_json(target["root"] / "podgrove.yml", target["config"])
        _, objects = self.start(target)
        endpoint = self.endpoint(source, target, objects)
        self.result["checks"][name + "-endpoint"] = endpoint
        self.pair(name + "-open-app", source, endpoint, allowed=True)
        self.pair(name + "-open-private", source, endpoint, allowed=True, port=18081)
        if source["namespace"] != target["namespace"]:
            self.mode(source, network_config("open"))
            self.pair(name + "-open-unlisted-namespace", source, endpoint, allowed=False)
        self.mode(source, network_config("disabled"))
        self.pair(name + "-disabled-egress", source, endpoint, allowed=False)
        self.mode(source, self.opened(source, target))
        self.mode(target, network_config("disabled"))
        self.pair(name + "-disabled-ingress", source, endpoint, allowed=False)
        selected_out = network_config("selected", peer_namespace=target["namespace"], worktree="apis-*")
        selected_in = network_config("selected", peer_namespace=source["namespace"], target=True, worktree="web-*")
        self.mode(source, network_config("selected"))
        self.mode(target, selected_in)
        self.pair(name + "-expose-only", source, endpoint, allowed=False)
        self.mode(target, network_config("selected"))
        self.mode(source, selected_out)
        self.pair(name + "-connect-only", source, endpoint, allowed=False)
        status = self.mode(target, selected_in)
        endpoints = [item for item in status.get("peer_endpoints", []) if item.get("service") == "app" and item.get("port") == 18080]
        reported = urlsplit(endpoints[0].get("url", "")) if len(endpoints) == 1 else None
        if (reported is None or reported.scheme != "http" or reported.hostname != endpoint["dns"]
                or reported.port != 18080 or reported.username or reported.password
                or reported.path not in ("", "/") or reported.query or reported.fragment):
            raise AcceptanceError("Selected service status must report its genuine engine DNS endpoint")
        self.pair(name + "-selected-mutual", source, endpoint, allowed=True)
        self.mode(source, self.opened(source, target))
        self.pair(name + "-open-source-selected-target", source, endpoint, allowed=False)
        self.mode(source, selected_out)
        self.mode(target, self.opened(target, source))
        self.pair(name + "-selected-source-open-target", source, endpoint, allowed=False)
        self.mode(target, selected_in)
        self.pair(name + "-mutual-restored", source, endpoint, allowed=True)
        self.pair(name + "-unexposed-port", source, endpoint, allowed=False, port=18081)
        self.mode(source, network_config("selected", peer_namespace=target["namespace"], worktree="absent-*"))
        self.pair(name + "-wrong-target-glob", source, endpoint, allowed=False)
        self.mode(source, selected_out)
        self.mode(target, network_config("selected", peer_namespace=source["namespace"], target=True, worktree="absent-*"))
        self.pair(name + "-wrong-source-glob", source, endpoint, allowed=False)
        self.mode(target, network_config("selected", peer_namespace=source["namespace"], target=True))
        self.pair(name + "-namespace-only-expose", source, endpoint, allowed=True)
        if not self.same_only:
            wrong = self.args.namespace_b if target["namespace"] == self.args.namespace_a else self.args.namespace_a
            self.mode(source, network_config("selected", peer_namespace=wrong, worktree="apis-*"))
            self.pair(name + "-wrong-target-namespace", source, endpoint, allowed=False)
            self.mode(source, selected_out)
            self.mode(target, network_config("selected", peer_namespace=self.args.namespace_b, target=True, worktree="web-*"))
            self.pair(name + "-wrong-source-namespace", source, endpoint, allowed=False)
        self.mode(target, selected_in)
        self.mode(source, network_config("selected", peer_namespace=target["namespace"], worktree="apis-*", ports=[18080, 18081]))
        self.pair(name + "-exposure-restricts-private-port", source, endpoint, allowed=False, port=18081)
        self.mode(target, network_config("selected", peer_namespace=source["namespace"], target=True, worktree="web-*", expose_private=True))
        self.mode(source, selected_out)
        self.pair(name + "-connection-restricts-private-port", source, endpoint, allowed=False, port=18081)
        self.mode(source, network_config("disabled"))
        self.pair(name + "-revoke-selected", source, endpoint, allowed=False)
        self.mode(target, self.opened(target, source))
        self.mode(source, self.opened(source, target))
        self.pair(name + "-reopen-app", source, endpoint, allowed=True)
        self.pair(name + "-reopen-private", source, endpoint, allowed=True, port=18081)

    def exercise(self):
        source, same = self.roles[:2]
        self.phase("same-namespace", source, same)
        self.runners["a"].cleanup_one(same)
        if not self.runners["a"].result["cleanup"][same["role"]].get("passed"):
            raise AcceptanceError("Same-namespace target cleanup must pass before a cross-namespace target can start")
        if not self.same_only:
            self.phase("cross-namespace", source, self.roles[2])

    def cleanup(self):
        for role in reversed(self.roles):
            self.runners[role["runner"]].cleanup_one(role)
        for key, runner in self.runners.items():
            self.result["cleanup"].update(runner.result["cleanup"])
            self.result.setdefault("command_evidence", {})[key] = {"path": str(runner.output), "commands": runner.commands}
        if not self.auth_stop and "bootstrap_before" in self.result:
            after = self.bootstrap()
            self.result["bootstrap_after"] = after
            if after != self.result["bootstrap_before"]:
                raise AcceptanceError("Shared provisioning markers changed during acceptance")
        if self.temp and len(self.result["cleanup"]) == 3 and all(entry.get("passed") for entry in self.result["cleanup"].values()):
            shutil.rmtree(self.temp)
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
            if self.auth_stop:
                self.result["authentication_stop"] = dict(self.auth_stop)
                self.result["remaining_unconfirmed"] = [{key: str(role[key]) for key in ("identity", "namespace", "state", "root")}
                                                         for role in self.roles if role["attempted"] and not role.get("cleanup_succeeded")]
            self.result.update(finished_at=time.time(), passed=completed and not self.result.get("cleanup_error")
                               and not self.auth_stop
                               and len(self.result["cleanup"]) == 3
                               and all(entry.get("passed") and not entry.get("diagnostic_error") for entry in self.result["cleanup"].values()))
            if self.created:
                base.write_json(self.output / "result.json", self.result)
        return 0 if self.result["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--podgrove-bin", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace-a", required=True)
    parser.add_argument("--namespace-b")
    parser.add_argument("--same-namespace-only", action="store_true", help="Run only namespace A; record cross-namespace checks as not run")
    parser.add_argument("--storage-class", required=True)
    parser.add_argument("--cluster-domain", help="Explicit DNS suffix if the Compose resolver lacks its Kubernetes search list")
    parser.add_argument("--settle-timeout", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("--execute is required; this test creates three disposable engines with at most two running together")
    if not args.podgrove_bin.is_absolute() or not args.podgrove_bin.is_file() or not os.access(args.podgrove_bin, os.X_OK):
        parser.error("--podgrove-bin must be an absolute installed executable")
    args.podgrove_bin = args.podgrove_bin.resolve()
    if not args.context or any(ord(char) < 32 or ord(char) == 127 for char in args.context):
        parser.error("--context must be explicit")
    if not args.same_namespace_only and args.namespace_b is None:
        parser.error("--namespace-b is required unless --same-namespace-only is selected")
    for namespace in (args.namespace_a, args.namespace_b):
        if namespace is None:
            continue
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace) or namespace.startswith("kube-"):
            parser.error("Both namespaces must be explicit approved non-system names")
    if args.namespace_a == args.namespace_b:
        parser.error("Two distinct existing prepared namespaces are required")
    if not valid_domain(args.storage_class) or (args.cluster_domain and not valid_domain(args.cluster_domain)):
        parser.error("Storage class and explicit cluster domain must be DNS names")
    if not 5 <= args.settle_timeout <= 180:
        parser.error("--settle-timeout must be between 5 and 180 seconds")
    if not args.output.is_absolute() or args.output.exists() or args.output.is_symlink() or not args.output.parent.is_dir():
        parser.error("--output must be a new absolute directory beneath an existing parent")
    return Runner(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
