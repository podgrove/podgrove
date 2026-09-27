#!/usr/bin/env python3
"""Validate adoption docs and example rendering without a cluster or Docker daemon."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote

import jsonschema
import yaml

from podgrove.config import CONFIG_SCHEMA

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, cwd=ROOT, env=None, input=None):
    result = subprocess.run(command, cwd=cwd, env=env, input=input, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"Documentation check failed: {command}\n{result.stderr[-2000:]}")
    return result.stdout


def slug(heading):
    return re.sub(r"[^\w\- ]", "", re.sub(r"[`*_]", "", heading.strip()).lower()).replace(" ", "-")


def verify():
    report = {"status": "running", "shell_blocks": 0, "links": 0, "schema_examples": 0, "rendered_examples": {},
              "live_cluster_access": False, "docker_daemon_access": False}
    public_files = json.loads((ROOT / "publication/public-files.json").read_text())["files"]
    names = [name for name in public_files if name.endswith(".md")]
    for name in names:
        doc = ROOT / name
        source = doc.read_text()
        for block in re.findall(r"```sh\n(.*?)```", source, re.S):
            run(["sh", "-n"], input=block)
            report["shell_blocks"] += 1
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", source):
            if "://" in target or target.startswith("mailto:"):
                continue
            path, _, anchor = unquote(target).partition("#")
            destination = (doc.parent / path).resolve() if path else doc
            assert destination.exists(), (doc, target)
            if anchor:
                headings = re.findall(r"^#+\s+(.+)$", destination.read_text(), re.M)
                assert anchor in {slug(heading) for heading in headings}, (doc, target)
            report["links"] += 1
    for path in (ROOT / "examples").rglob("podgrove*.yml"):
        jsonschema.validate(yaml.safe_load(path.read_text()), CONFIG_SCHEMA)
        report["schema_examples"] += 1
    with tempfile.TemporaryDirectory(prefix="podgrove-doc-check-") as directory:
        scratch = Path(directory).resolve()
        environment = {**os.environ, "KUBECONFIG": str(scratch / "absent-kubeconfig"),
                       "DOCKER_HOST": "tcp://127.0.0.1:1", "PODGROVE_STATE_HOME": str(scratch / "absent-state")}
        binary = [sys.executable, "-m", "podgrove"]
        # Editable/installable module, explicit project-directory avoids PYTHONPATH changes.
        for example in ("shared", "worktree", "network", "resources"):
            root = scratch / example
            root.mkdir()
            shutil.copyfile(ROOT / "examples" / example / "podgrove.yml", root / "podgrove.yml")
            if example == "resources":
                shutil.copyfile(ROOT / "examples" / example / "compose.yml", root / "compose.yml")
            else:
                (root / "compose.yaml").write_text('services:\n  app:\n    image: alpine:3.21\n    command: ["sleep", "60"]\n')
            flags = ["--project-directory", str(root)]
            run([*binary, "validate", *flags], env=environment)
            rendered = json.loads(run([*binary, "up", "--dry-run", *flags], env=environment))
            output = scratch / (example + "-bootstrap")
            run([*binary, "bootstrap", *flags, "--output", str(output)], env=environment)
            objects = [body for file in output.glob("*.yaml") for body in yaml.safe_load_all(file.read_text())]
            assert len(objects) == 9
            assert {body["kind"] for body in objects} == {"ConfigMap", "NetworkPolicy", "ServiceAccount", "Role", "RoleBinding"}
            assert all(body["metadata"]["namespace"] == rendered["namespace"] for body in objects)
            marker, = [body for body in objects if body["kind"] == "ConfigMap"]
            assert marker["data"]["namespace_mode"] == rendered["namespace_mode"]
            assert next(body for body in rendered["resources"] if body["kind"] == "PersistentVolumeClaim")["spec"]["storageClassName"] == "your-delete-storage-class"
            if example == "resources":
                controller = next(body for body in rendered["resources"] if body["kind"] == "StatefulSet")
                budget = controller["spec"]["template"]["spec"]["containers"][0]["resources"]
                assert budget["requests"] == {"cpu": "1500m", "memory": "6Gi", "ephemeral-storage": "1Gi"}
                assert budget["limits"] == {"cpu": "6", "memory": "12Gi", "ephemeral-storage": "8Gi"}
                assert next(body for body in rendered["resources"] if body["kind"] == "PersistentVolumeClaim")["spec"]["resources"]["requests"]["storage"] == "40Gi"
            report["rendered_examples"][example] = {"validate": True, "dry_run": True, "bootstrap_objects": len(objects)}
        assert not (scratch / "absent-state").exists()
    report["status"] = "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New report file; existing evidence is preserved")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output must not already exist")
    report = verify()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as target:
        json.dump(report, target, indent=2)
        target.write("\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
