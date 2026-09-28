#!/usr/bin/env python3
"""Opt-in acceptance of partial startup, protection repair and mid-build recovery."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
import uuid

_SPEC = importlib.util.spec_from_file_location("podgrove_connectivity_acceptance", Path(__file__).with_name("check_connectivity.py"))
base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(base)
AcceptanceError = base.AcceptanceError
ANNOTATIONS = ("cluster-autoscaler.kubernetes.io/safe-to-evict", "autoscaling.cast.ai/removal-disabled")
BODY = b"podgrove-recovery-fixture\n"
BUILD_PROGRAM = "import time;time.sleep(90)"
UID_GUARD = 'test "${PODGROVE_POD_UID:-}" = "$1" || exit 126; shift; exec "$@"'
PROCESS_PROBE = r'''expected=$(printf '%s\n' python -c "$1" "$2")
for item in /proc/[0-9]*/cmdline; do
  actual=$(tr '\000' '\n' < "$item" 2>/dev/null) || continue
  if [ "$actual" = "$expected" ]; then printf '%s\n' "$item"; fi
done'''


def stop_child(process):
    if process.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                raise
        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            if sig == signal.SIGKILL:
                raise


def annotation_patch(resource, key):
    metadata = resource["metadata"]
    prefix = "/spec/template/metadata/annotations/" if resource["kind"] == "StatefulSet" else "/metadata/annotations/"
    target = resource["spec"]["template"]["metadata"] if resource["kind"] == "StatefulSet" else metadata
    expected = "false" if key == ANNOTATIONS[0] else "true"
    if key not in ANNOTATIONS or target.get("annotations", {}).get(key) != expected:
        raise AcceptanceError("Expected protection annotation is absent before its mutation test")
    if not metadata.get("uid") or not metadata.get("resourceVersion"):
        raise AcceptanceError("Mutation requires fresh UID and resourceVersion")
    path = prefix + key.replace("~", "~0").replace("/", "~1")
    return [{"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
            {"op": "test", "path": path, "value": expected}, {"op": "remove", "path": path}]


def service_ids(value):
    rows = value.get("services", [])
    result = {}
    for row in rows:
        name, ident = row.get("Service"), row.get("ID", "")
        if name not in ("healthy", "exited", "unhealthy") or name in result or not re.fullmatch(r"[a-f0-9]{12,64}", ident):
            raise AcceptanceError("Service container identity inventory is incomplete or ambiguous")
        result[name] = ident
    if set(result) != {"healthy", "exited", "unhealthy"}:
        raise AcceptanceError("Expected all three fixture containers")
    return result


def prove_inventory(payload, role, namespace):
    proof = base.checked_objects(payload, namespace, role["identity"])
    captured = role.get("captured", {})
    allowed = role.get("replacement_pending", set())
    for key, uid in captured.items():
        if key in proof and proof[key] != uid and key not in allowed:
            raise AcceptanceError("An unplanned fixture resource replacement occurred")
    name = "pg-" + role["identity"]
    objects = {item["kind"]: item for item in payload["items"] if item["metadata"]["name"] in (name, name + "-0")}
    if allowed and proof:
        for kind in ("StatefulSet", "PersistentVolumeClaim"):
            key = kind + "/" + name
            if not captured.get(key) or proof.get(key) != captured[key]:
                raise AcceptanceError("Original controller and PVC must survive the deliberate replacement")
    if "Pod" in objects:
        controller = objects.get("StatefulSet", {})
        pod = objects["Pod"]
        owner_uid = controller.get("metadata", {}).get("uid")
        owners = pod["metadata"].get("ownerReferences", [])
        if not owner_uid or not any(owner.get("kind") == "StatefulSet" and owner.get("apiVersion") == "apps/v1"
                and owner.get("name") == name and owner.get("uid") == owner_uid and owner.get("controller") is True for owner in owners):
            raise AcceptanceError("Fixture Pod does not belong to the original controller")
        claims = [volume["persistentVolumeClaim"].get("claimName") for volume in pod.get("spec", {}).get("volumes", [])
                  if "persistentVolumeClaim" in volume]
        if claims != [name]:
            raise AcceptanceError("Fixture Pod does not use exactly its owned PVC")
    return proof


class Runner(base.Runner):
    def prepare(self):
        self.output.mkdir(mode=0o700)
        self.output_created = True
        self.base = Path(tempfile.mkdtemp(prefix="podgrove-startup-"))
        if any((parent / ".git").exists() for parent in (self.base, *self.base.parents)):
            raise AcceptanceError("Fixture temp parent is inside a Git worktree")
        version, _ = self.command([str(self.args.podgrove_bin), "--version"])
        self.result.update(binary=str(self.args.podgrove_bin), version=version.strip(),
                           binary_sha256=hashlib.sha256(self.args.podgrove_bin.read_bytes()).hexdigest(),
                           fixture_base=str(self.base), startup_phase_timeout_seconds=600, up_wait_limit_seconds=1980,
                           proof_scope="Three serial disposable engines; no Node, namespace or storage-backend mutations")
        fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/recovery"
        paths = ("compose.yml", "server.py", "content/seed.txt")
        self.result["fixture_sources"] = {name: hashlib.sha256((fixture / name).read_bytes()).hexdigest() for name in paths}
        for name in ("ordinary", "refresh", "midbuild"):
            root, state = self.base / name, self.base / (name + "-state")
            (root / "content").mkdir(mode=0o700, parents=True)
            root.chmod(0o700)
            for source in paths:
                shutil.copyfile(fixture / source, root / source)
            state.mkdir(mode=0o700)
            role = {"role": name, "root": root, "state": state, "identity": base.identity(root), "attempted": False}
            config = base.fixture_config(self.args.context, self.args.namespace, self.args.storage_class)
            config["ttl"] = "2h"
            base.write_json(root / "podgrove.yml", config)
            self.fixtures.append(role)
            _, proof = self.inventory(role)
            if proof:
                raise AcceptanceError("Fresh fixture identity already exists; refusing adoption")
            self.cli(role, "validate", "--json")
        self.result["fixtures"] = [{"role": r["role"], "root": str(r["root"]), "identity": r["identity"],
                                    "state": str(r["state"])} for r in self.fixtures]

    def inventory(self, role):
        selector = f"{base.MANAGED}=podgrove,{base.ENVIRONMENT}={role['identity']}"
        raw, _ = self.command(self.kubectl("get", base.KINDS, "-l", selector, "-o", "json"), timeout=30)
        payload = json.loads(raw)
        return payload, prove_inventory(payload, role, self.args.namespace)

    def kubectl(self, *args):
        return ["kubectl", "--context", self.args.context, "--namespace", self.args.namespace,
                "--request-timeout=20s", *args]

    def capture(self, role):
        payload, proof = self.inventory(role)
        role.setdefault("captured", {}).update(proof)
        return payload

    def state_file(self, role):
        suffix = hashlib.sha256(self.args.context.encode()).hexdigest()[:8]
        return role["state"] / (role["identity"] + "-" + suffix + ".json")

    def observe(self, role):
        raw, _ = self.cli(role, "status", "--json", timeout=45, check=False)
        value = json.loads(raw)
        if (value.get("identity") != role["identity"] or value.get("context") != self.args.context
                or value.get("namespace") != self.args.namespace):
            raise AcceptanceError("Status belongs to another fixture")
        pid = value.get("pid")
        if type(pid) is int and pid > 1:
            description, code = self.command(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="], check=False)
            if code or not description.strip().endswith(" -I -B -m podgrove _serve " + str(self.state_file(role))):
                raise AcceptanceError("Supervisor is not bound to this exact fixture state")
            captured = {"pid": pid, "description": description.strip()}
            if captured not in role.setdefault("processes", []):
                role["processes"].append(captured)
        return value

    def run_up(self, role, *, refresh=False, partial=False):
        role["attempted"] = True
        raw, code = self.cli(role, "up", "--json", "--timeout", "600", *(["--refresh"] if refresh else []),
                             timeout=1980, check=False)
        value = json.loads(raw)
        if code != (1 if partial else 0) or value.get("startup_status", {}).get("state") != ("failed" if partial else "ready"):
            raise AcceptanceError("Up did not report the expected partial/ready startup result")
        value = self.observe(role)
        if partial and value.get("status") not in ("unhealthy", "degraded") or not partial and value.get("status") != "ready":
            raise AcceptanceError("Observed runtime status contradicts startup result")
        self.capture(role)
        return value

    def local_http(self, value):
        ports = [item for item in value.get("ports", []) if item.get("service") == "healthy" and item.get("status") == "ready"]
        if len(ports) != 1 or type(ports[0].get("local")) is not int:
            raise AcceptanceError("Healthy service has no unique ready loopback forward")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{ports[0]['local']}/", timeout=5) as response:
            if response.status != 200 or response.read(4096) != BODY:
                raise AcceptanceError("Healthy service's loopback HTTP response did not match")

    def partial_case(self, role, *, refresh=False):
        value = self.run_up(role, partial=True)
        deadline = time.monotonic() + 30
        while True:
            rows = {row.get("Service"): row for row in value.get("services", [])}
            if rows.get("exited", {}).get("State") == "exited" and rows.get("unhealthy", {}).get("Health") == "unhealthy":
                break
            if time.monotonic() >= deadline:
                raise AcceptanceError("Partial fixture did not expose both the exited and unhealthy states")
            time.sleep(.5)
            value = self.observe(role)
        before = service_ids(value)
        forwarded = {item.get("service") for item in value.get("ports", [])}
        running = {name for name, row in rows.items() if row.get("State") == "running"}
        if "healthy" not in forwarded or not forwarded <= running or "exited" in forwarded:
            raise AcceptanceError("Partial startup lacks its healthy forward or exposes an exited service")
        self.local_http(value)
        raw, _ = self.cli(role, "exec", "healthy", "--", "python", "-c", "print('diagnostic-ready')")
        if raw.strip() != "diagnostic-ready":
            raise AcceptanceError("Healthy service exec did not remain available after partial startup")
        (role["root"] / "content/required.txt").write_text("fixture recovery\n")
        value = self.run_up(role, refresh=refresh)
        after = service_ids(value)
        if before["healthy"] != after["healthy"] or any(before[name] == after[name] for name in ("exited", "unhealthy")):
            raise AcceptanceError("Recovery failed to preserve healthy service or recreate failed services")
        self.local_http(value)
        self.result["checks"][role["role"]] = {"partial_exit": 1, "ready_exit": 0, "before_ids": before, "after_ids": after,
                                              "healthy_http": True, "diagnostic_exec": True}

    def object(self, role, kind):
        payload = self.capture(role)
        values = [item for item in payload["items"] if item["kind"] == kind]
        if len(values) != 1:
            raise AcceptanceError("Expected one exact owned " + kind)
        value = values[0]
        if value["metadata"].get("deletionTimestamp") or not value["metadata"].get("resourceVersion"):
            raise AcceptanceError("Fixture mutation target is deleting or lacks resourceVersion")
        return value

    def doctor_refuses(self, role):
        raw, code = self.cli(role, "doctor", "--json", check=False)
        value = json.loads(raw)
        if (code != 1 or value.get("command") != "doctor" or value.get("status") != "error"
                or "Existing engine eviction protection is missing or changed" not in value.get("error", "")):
            raise AcceptanceError("Doctor failed to detect the deliberately missing safeguard")

    def delete_exact(self, role, resource):
        kind, metadata = resource["kind"], resource["metadata"]
        if kind not in ("Pod", "PodDisruptionBudget"):
            raise AcceptanceError("Only the explicitly tested Pod or PDB may be removed")
        fresh = self.object(role, kind)
        if any(fresh["metadata"].get(key) != metadata.get(key) for key in ("name", "namespace", "uid", "resourceVersion")):
            raise AcceptanceError("Mutation target changed before its preconditioned deletion")
        stem = f"delete-{role['role']}-{kind}-{uuid.uuid4().hex}.json"
        options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": metadata["uid"],
                   "resourceVersion": metadata["resourceVersion"]}}
        if kind == "Pod":
            options["gracePeriodSeconds"] = 30
        path = self.output / stem
        base.write_json(path, options)
        prefix = "/api/v1" if kind == "Pod" else "/apis/policy/v1"
        plural = "pods" if kind == "Pod" else "poddisruptionbudgets"
        raw_path = f"{prefix}/namespaces/{self.args.namespace}/{plural}/{metadata['name']}"
        role.setdefault("replacement_pending", set()).add(kind + "/" + metadata["name"])
        self.command(self.kubectl("delete", "--raw", raw_path, "-f", str(path)), role=role, timeout=45)

    def protection_case(self, role):
        checks = []
        for kind in ("Pod", "StatefulSet"):
            for key in ANNOTATIONS:
                obj = self.object(role, kind)
                patch = annotation_patch(obj, key)
                self.command(self.kubectl("patch", kind, obj["metadata"]["name"], "--type=json", "-p", json.dumps(patch)), role=role)
                self.doctor_refuses(role)
                self.run_up(role)
                repaired = self.object(role, kind)
                annotation_patch(repaired, key)
                checks.append({"kind": kind, "annotation": key, "detected_and_repaired": True})
        old = self.object(role, "PodDisruptionBudget")
        self.delete_exact(role, old)
        self.doctor_refuses(role)
        self.run_up(role)
        repaired = self.object(role, "PodDisruptionBudget")
        if repaired["metadata"]["uid"] == old["metadata"]["uid"] or repaired.get("spec") != old.get("spec"):
            raise AcceptanceError("Up did not recreate the exact intended disruption budget")
        role["replacement_pending"].clear()
        self.result["checks"]["protection"] = {"annotations": checks, "pdb_detected_and_repaired": True}

    def build_is_running(self, role, pod_uid, nonce):
        argv = self.kubectl("exec", "--request-timeout=15s", "pg-" + role["identity"] + "-0", "-c", "docker", "--",
                            "sh", "-c", UID_GUARD, "podgrove-acceptance-guard", pod_uid,
                            "sh", "-c", PROCESS_PROBE, "podgrove-build-probe", BUILD_PROGRAM, nonce)
        raw, code = self.command(argv, role=role, timeout=20, check=False)
        matches = raw.strip().splitlines()
        return code == 0 and bool(matches) and all(re.fullmatch(r"/proc/[0-9]+/cmdline", item) for item in matches)

    def midbuild_case(self, role):
        (role["root"] / "content/required.txt").write_text("fixture initially ready\n")
        nonce = uuid.uuid4().hex
        (role["root"] / "Dockerfile").write_text("FROM python:3.12-alpine\nRUN " + json.dumps(["python", "-c", BUILD_PROGRAM, nonce]) + "\n")
        base.write_json(role["root"] / "build.json", {"services": {"healthy": {"build": {"context": "."},
                        "image": "podgrove-acceptance-" + nonce}}})
        config = json.loads((role["root"] / "podgrove.yml").read_text())
        config["compose"]["files"].append("build.json")
        base.write_json(role["root"] / "podgrove.yml", config)
        role["attempted"] = True
        outpath, errpath = self.output / "midbuild-up.stdout", self.output / "midbuild-up.stderr"
        argv = [str(self.args.podgrove_bin), "up", "--project-directory", str(role["root"]), "--context", self.args.context,
                "--namespace", self.args.namespace, "--json", "--timeout", "600"]
        record = {"argv": argv, "stdout": outpath.name, "stderr": errpath.name, "started_at": time.time()}
        self.commands.append(record)
        with outpath.open("xb") as out, errpath.open("xb") as err:
            process = subprocess.Popen(argv, cwd=role["root"], env={**os.environ, "PODGROVE_STATE_HOME": str(role["state"])},
                                       stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
            total_deadline = time.monotonic() + 1980
            try:
                deadline = time.monotonic() + 600
                while True:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise AcceptanceError("Startup did not reach a provably running build before injection")
                    logs = list(role["state"].glob("*.log"))
                    if logs and "Startup: building and starting Compose services" in logs[0].read_text(errors="replace"):
                        pod = self.object(role, "Pod")
                        if self.build_is_running(role, pod["metadata"]["uid"], nonce):
                            break
                    time.sleep(.5)
                before = dict(role["captured"])
                self.observe(role)
                self.delete_exact(role, pod)
                process.wait(timeout=max(.1, total_deadline - time.monotonic()))
                if process.returncode != 0:
                    raise AcceptanceError("Mid-build replacement did not complete up successfully")
                value = self.observe(role)
                if value.get("status") != "ready" or value.get("startup_status", {}).get("attempts", 0) < 1:
                    raise AcceptanceError("No successful bounded startup replacement retry was observed")
                self.capture(role)
                if role["captured"].get("Pod/pg-" + role["identity"] + "-0") == pod["metadata"]["uid"]:
                    raise AcceptanceError("Engine Pod was not replaced")
                role["replacement_pending"].clear()
                self.local_http(value)
                self.result["checks"]["midbuild"] = {"nonce": nonce, "actual_build_observed": True,
                    "before": before, "after": dict(role["captured"]), "startup_status": value["startup_status"], "healthy_http": True}
            finally:
                stop_child(process)
                record.update(returncode=process.returncode, finished_at=time.time())

    def cleanup_role(self, role):
        if role.get("cleaned"):
            return
        original_fixtures, original_base = self.fixtures, self.base
        self.fixtures, self.base = [role], None
        try:
            base.Runner.cleanup(self)
        finally:
            self.fixtures, self.base = original_fixtures, original_base
        role["cleaned"] = self.result["cleanup"][role["role"]].get("passed", False)

    def cleanup(self):
        for role in reversed(self.fixtures):
            self.cleanup_role(role)
        if self.base and self.fixtures and all(role.get("cleaned") for role in self.fixtures):
            shutil.rmtree(self.base)
            self.result["fixture_directories_removed"] = True

    def exercise(self):
        for role in self.fixtures:
            if role["role"] == "midbuild":
                self.midbuild_case(role)
            else:
                self.partial_case(role, refresh=role["role"] == "refresh")
                if role["role"] == "ordinary":
                    self.protection_case(role)
            self.cleanup_role(role)
            if not role["cleaned"]:
                raise AcceptanceError("Serial fixture cleanup failed; refusing to start another engine")


def main(argv=None):
    command = base.parser()
    command.description = __doc__
    args = command.parse_args(argv)
    if not args.execute:
        print(json.dumps({"execute": False, "plan": "Create three serial disposable recovery engines; test partial startup, guarded safeguards and one observed mid-build Pod replacement; clean up exact fixtures."}))
        return 0
    if not args.podgrove_bin.is_absolute() or not args.podgrove_bin.is_file() or not os.access(args.podgrove_bin, os.X_OK):
        command.error("--podgrove-bin must be an absolute executable path")
    args.podgrove_bin = args.podgrove_bin.resolve()
    if not args.context or any(ord(ch) < 32 for ch in args.context):
        command.error("--context must be explicit")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", args.namespace) or args.namespace.startswith("kube-"):
        command.error("--namespace must be an approved explicit non-system namespace")
    if not args.output.is_absolute() or args.output.exists() or args.output.is_symlink() or not args.output.parent.is_dir():
        command.error("--output must be a new absolute directory beneath an existing parent")
    if not args.storage_class or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", args.storage_class):
        command.error("--storage-class must be explicit")
    return Runner(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
