#!/usr/bin/env python3
"""Opt-in live startup diagnostics, progress, non-root sync and recovery acceptance."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request

_SPEC = importlib.util.spec_from_file_location("startup_acceptance", Path(__file__).with_name("check_startup_recovery.py"))
recovery = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(recovery)
base = recovery.base


def build_is_streaming(output):
    began = re.search(r"^(?:#\d+ [\d.]+ )?build probe started$", output, re.MULTILINE)
    finished = re.search(r"^(?:#\d+ [\d.]+ )?build probe finished$", output, re.MULTILINE)
    return bool(began and not finished)


class Runner(recovery.Runner):
    def prepare(self):
        self.output.mkdir(mode=0o700)
        self.output_created = True
        self.base = Path(tempfile.mkdtemp(prefix="podgrove-startup-diagnostics-")).resolve()
        if any((parent / ".git").exists() for parent in (self.base, *self.base.parents)):
            raise base.AcceptanceError("Fresh fixture parent must be outside every Git worktree")
        root, state = self.base / "fixture", self.base / "state"
        root.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        role = {"role": "startup", "root": root, "state": state, "identity": base.identity(root), "attempted": False}
        self.fixtures.append(role)
        fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/startup-diagnostics"
        for name in ("Dockerfile", "compose.yml", "server.py"):
            shutil.copyfile(fixture / name, root / name)
        for path in (root / "content", root / "content/shared", root / "content/shared/middleware"):
            path.mkdir(mode=0o700)
        value = root / "content/shared/middleware/value.txt"
        value.write_text("readable\n")
        value.chmod(0o600)
        (root / "server.py").chmod(0o600)
        base.write_json(root / "podgrove.yml", base.fixture_config(self.args.context, self.args.namespace, self.args.storage_class))
        self.result.update(fixture_base=str(self.base), binary=str(self.args.podgrove_bin),
                           proof_scope="One disposable engine in the explicit namespace; no existing worktrees changed",
                           fixtures=[{"root": str(root), "identity": role["identity"], "state": str(state)}])
        _, present = self.inventory(role)
        if present:
            raise base.AcceptanceError("Fresh fixture identity already exists")
        self.cli(role, "validate", "--json")

    def observe_startup(self, role):
        role["attempted"] = True
        outpath, errpath = self.output / "up.stdout", self.output / "up.stderr"
        argv = [str(self.args.podgrove_bin), "up", "--project-directory", str(role["root"]),
                "--context", self.args.context, "--namespace", self.args.namespace, "--timeout", "600"]
        record = {"argv": argv, "stdout": outpath.name, "stderr": errpath.name, "started_at": time.time()}
        self.commands.append(record)
        progressing, streamed = [], False
        with outpath.open("xb") as out, errpath.open("xb") as err:
            process = subprocess.Popen(argv, cwd=role["root"], env={**os.environ, "PODGROVE_STATE_HOME": str(role["state"])},
                                       stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                deadline = time.monotonic() + 1980
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        raise base.AcceptanceError("Live acceptance startup deadline expired")
                    if list(role["state"].glob("*.json")):
                        raw, code = self.cli(role, "status", "--json", check=False, timeout=45)
                        value = json.loads(raw)
                        if value.get("pid") and not role.get("processes"):
                            self.observe(role)
                            self.capture(role)
                        if value.get("status") == "starting" and value.get("startup_status", {}).get("state") != "failed":
                            progress = value.get("startup_progress", {})
                            if code or not progress.get("phase") or not isinstance(progress.get("elapsed_seconds"), (int, float)):
                                raise base.AcceptanceError("Normal startup status must succeed and include phase and elapsed time")
                            progressing.append(progress)
                        output = errpath.read_text(errors="replace")
                        if build_is_streaming(output) and process.poll() is None:
                            streamed = True
                    time.sleep(2)
                record["returncode"] = process.returncode
                if process.returncode == 0:
                    raise base.AcceptanceError("Partial startup must return nonzero")
            finally:
                recovery.stop_child(process)
                record["finished_at"] = time.time()
        output = outpath.read_text() + errpath.read_text()
        for name in ("exited", "unhealthy"):
            if name + ": missing /fixture/required.txt" not in output:
                raise base.AcceptanceError("Partial startup omitted the failing service's actionable log: " + name)
        if not progressing or not streamed or not any(
                row.get("state") == "building" for value in progressing for row in value.get("services", [])):
            raise base.AcceptanceError("No per-service build status and pre-completion streamed output were observed")
        self.result["checks"]["startup_progress"] = {"observations": progressing, "output_before_exit": streamed,
                                                     "partial_exit": record["returncode"]}

    def exercise(self):
        role = self.fixtures[0]
        self.observe_startup(role)
        raw, code = self.cli(role, "status", "--json", check=False)
        value = json.loads(raw)
        diagnostics = value.get("startup_diagnostics", [])
        failures = {item["service"]: item for item in diagnostics}
        if code == 0 or set(failures) != {"exited", "unhealthy"}:
            raise base.AcceptanceError("Partial status must identify exactly the two failed services")
        for item in failures.values():
            if len(item.get("logs", [])) != 40:
                raise base.AcceptanceError("Each failed service must include its last 40 log lines")
            if len(item.get("containers", [])) != 1:
                raise base.AcceptanceError("Each failing fixture service must report exactly one container")
            for container in item["containers"]:
                if any(key not in container for key in ("state", "exit_code", "restart_count")):
                    raise base.AcceptanceError("Partial diagnostics omitted container state/exit/restarts")
                if container["restart_count"] != 0:
                    raise base.AcceptanceError("Fixture restart count was not observed correctly")
        if failures["exited"]["containers"][0]["exit_code"] != 7:
            raise base.AcceptanceError("Exited fixture must report its actual exit code 7")
        if failures["unhealthy"]["containers"][0]["health"] != "unhealthy":
            raise base.AcceptanceError("Unhealthy fixture must report its actual health")
        self.result["checks"]["partial_diagnostics"] = diagnostics
        script = "import os,pathlib;assert os.geteuid()==1000;assert pathlib.Path('/fixture/shared/middleware/value.txt').read_text()=='readable\\n';print('non-root read passed')"
        self.cli(role, "exec", "readable", "--", "python", "-c", script)
        self.result["checks"]["non_root_read_and_partial_exec"] = True
        (role["root"] / "content/required.txt").write_text("ready\n")
        (role["root"] / "content/required.txt").chmod(0o600)
        text, _ = self.cli(role, "up", "--timeout", "600", timeout=1980)
        ready = self.status(role)
        self.observe(role)
        self.capture(role)
        ports = ready.get("ports", [])
        if {port["service"] for port in ports} != {"readable", "exited", "unhealthy"}:
            raise base.AcceptanceError("Healthy run did not expose all fixture services")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for port in ports:
            address = f"http://127.0.0.1:{port['local']}"
            if str(port["local"]) not in text:
                raise base.AcceptanceError("Healthy human output omitted an endpoint")
            with opener.open(address, timeout=10) as response:
                if response.read() != b"podgrove-diagnostics-fixture\n":
                    raise base.AcceptanceError("Endpoint returned the wrong fixture")
        for path in (role["root"] / "content", role["root"] / "content/shared/middleware"):
            if path.stat().st_mode & 0o777 != 0o700:
                raise base.AcceptanceError("Sync changed a local directory's permissions")
        if (role["root"] / "content/shared/middleware/value.txt").stat().st_mode & 0o777 != 0o600:
            raise base.AcceptanceError("Sync changed local file permissions")
        self.result["checks"]["plain_up_recovery_and_endpoints"] = ports
        self.result["checks"]["local_permissions_unchanged"] = True


def main(argv=None):
    parser = base.parser()
    parser.description = __doc__
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"execute": False, "plan": "Create one disposable engine; check restricted sync, partial diagnostics, build progress and recovery; clean up."}))
        return 0
    if not args.podgrove_bin.is_absolute() or not args.podgrove_bin.is_file() or not os.access(args.podgrove_bin, os.X_OK):
        parser.error("--podgrove-bin must be an absolute executable path")
    if not args.context or any(ord(ch) < 32 for ch in args.context):
        parser.error("--context must be explicit")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", args.namespace) or args.namespace.startswith("kube-"):
        parser.error("--namespace must be an approved explicit non-system namespace")
    if not args.storage_class or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", args.storage_class):
        parser.error("--storage-class must be explicit")
    if not args.output.is_absolute() or args.output.exists() or args.output.is_symlink() or not args.output.parent.is_dir():
        parser.error("--output must be a new absolute directory beneath an existing parent")
    return Runner(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
