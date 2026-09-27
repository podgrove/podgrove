#!/usr/bin/env python3
"""Build and verify an isolated installed package without network/cluster access.

Run after source/docs freeze. --output must be a new directory; --deliver also
archives the old matching dist pair before replacing it with verified archives.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.parser import BytesParser
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import stat
import tomllib

import yaml

REPO = Path(__file__).resolve().parents[1]
BASE: Path
DIST: Path
report: dict


def prepare_output(output: Path) -> Path:
    path = Path(os.path.abspath(output.expanduser()))
    if path.exists() or path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Use a new ordinary output directory; existing evidence is never overwritten")
    if not path.parent.is_dir():
        raise ValueError("The output parent directory must already exist")
    path.mkdir(mode=0o700)
    return path


def source_snapshot() -> dict[str, bytes]:
    from scripts.prepare_public import snapshot_sources
    return {item.path: item.content for item in snapshot_sources(REPO)}


def cached_build_backend() -> tuple[Path, str]:
    candidates = []
    try:
        distribution = metadata.distribution("setuptools")
        candidates.append(Path(distribution.locate_file("")))
    except metadata.PackageNotFoundError:
        pass
    cache = Path.home() / ".cache/uv/archive-v0"
    if cache.is_dir():
        candidates.extend(sorted(cache.iterdir()))
    for candidate in candidates:
        if (candidate / "setuptools/__init__.py").is_file():
            for distribution in metadata.distributions(path=[str(candidate)]):
                if distribution.metadata["Name"] == "setuptools" and int(distribution.version.split(".")[0]) >= 77:
                    return candidate, distribution.version
    raise RuntimeError("A locally installed/cached setuptools >=75 is required; this verifier never downloads it")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def run(args, *, cwd, env=None, log=None, expected=0):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if log:
        (BASE / log).write_text(result.stdout + result.stderr)
    assert result.returncode == expected, (args, result.returncode, result.stdout[-2000:], result.stderr[-2000:])
    return result.stdout, result.stderr


def verify():
    assert not DIST.exists(), "Use a new artifact directory for each build checkpoint"
    DIST.mkdir()
    # Reuse a cached build backend by copying its installed files. No package
    # resolver, network connection or write to the user's global cache occurs.
    backend, version = cached_build_backend()
    copied_backend = BASE / "build-backend"
    # Copy only the selected distribution's own files; never a whole user site.
    copied_backend.mkdir()
    backend_dist = next(item for item in metadata.distributions(path=[str(backend)])
                        if item.metadata["Name"] == "setuptools")
    for entry in backend_dist.files or []:
        relative = Path(entry)
        if ".." in relative.parts or "__pycache__" in relative.parts or relative.suffix == ".pyc":
            continue
        origin = backend / relative
        if origin.is_file():
            target = copied_backend / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
    report["build_backend"] = {"name": "setuptools", "version": version, "copied_from": str(backend)}
    snapshot = source_snapshot()
    project = tomllib.loads(snapshot["pyproject.toml"].decode())["project"]
    source_prefix = f"{project['name']}-{project['version']}"
    report["source_sha256"] = {name: digest(raw) for name, raw in sorted(snapshot.items())}
    build = ("import sys;sys.path.insert(0," + repr(str(copied_backend)) + ");"
             "from setuptools import build_meta;print(build_meta.build_sdist(" + repr(str(DIST)) + "))")
    run([sys.executable, "-I", "-c", build], cwd=REPO, log="build-sdist.log")
    sdist, = DIST.glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        names = set(archive.getnames())
        prefix = source_prefix + "/"
        assert prefix + "podgrove.yml" not in names
        assert not any(name.startswith(prefix + "deploy/bootstrap/") for name in names)
        assert not any(name.startswith(prefix + "deploy/optional-tainted-nodes/") for name in names)
        for name, raw in snapshot.items():
            assert archive.extractfile(prefix + name).read() == raw, name
        assert prefix + "deploy/reaper/00-kubeconfig.yaml.example" in names
        assert prefix + "deploy/reaper/10-cronjob.yaml.example" in names
        assert prefix + "deploy/reaper/20-network-policy.yaml.example" in names
        assert prefix + "podgrove/network.py" in names
        assert prefix + "examples/worktree/podgrove.yml" in names
        assert prefix + "examples/dashboard/podgrove.yml" in names
        assert not any(name.startswith(prefix + "artifacts/") for name in names)
        archive.extractall(BASE / "source", filter="data")
    report["checks"]["sdist_source_templates_docs_examples_parity"] = True
    source = BASE / "source" / source_prefix
    build = ("import sys;sys.path.insert(0," + repr(str(copied_backend)) + ");"
             "from setuptools import build_meta;print(build_meta.build_wheel(" + repr(str(DIST)) + "))")
    run([sys.executable, "-I", "-c", build], cwd=source, log="build-wheel.log")
    wheel, = DIST.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert "podgrove/bootstrap.py" in names
        assert "podgrove/network.py" in names
        assert "podgrove/repository.py" in names and "podgrove/session_status.py" in names
        assert "podgrove/web_settings.py" in names and "podgrove/sync_transport.py" in names
        assert "podgrove/sync_filter.py" in names
        for name, raw in snapshot.items():
            if name.startswith("podgrove/"):
                assert archive.read(name) == raw, name
        header = BytesParser().parsebytes(archive.read(next(name for name in names if name.endswith(".dist-info/METADATA"))))
        assert header.get_payload().rstrip() == snapshot["README.md"].decode().rstrip()
        assert all(not name.startswith(("tests/", "artifacts/", "deploy/")) for name in names)
        assert "podgrove.yml" not in names
    report["checks"]["wheel_modules_assets_readme_parity"] = True
    report["checks"]["workspace_cluster_config_excluded"] = True
    report["archives"] = {path.name: {"sha256": digest(path.read_bytes()), "bytes": path.stat().st_size}
                          for path in (wheel, sdist)}
    with tempfile.TemporaryDirectory(prefix="podgrove-portability-wheel-") as temporary:
        work = Path(temporary).resolve()
        isolated = work / "venv"
        app = work / "app"
        app.mkdir()
        run([sys.executable, "-I", "-m", "venv", "--without-pip", isolated], cwd=work, log="create-venv.log")
        python = isolated / "bin/python"
        site = next((isolated / "lib").glob("python*/site-packages"))
        dependencies = []
        for name in ("PyYAML", "jsonschema", "attrs", "jsonschema-specifications", "referencing", "rpds-py", "typing-extensions"):
            distribution = metadata.distribution(name)
            original_site = Path(distribution.locate_file("")).resolve()
            copied = 0
            for entry in distribution.files:
                relative = Path(entry)
                if ".." in relative.parts or "__pycache__" in relative.parts or relative.suffix == ".pyc":
                    continue
                origin = Path(distribution.locate_file(entry)).resolve()
                assert origin.is_relative_to(original_site), origin
                if not origin.is_file():
                    continue
                destination = site / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, destination)
                copied += 1
            dependencies.append({"name": name, "version": distribution.version, "copied_files": copied})
        report["dependencies"] = dependencies
        build_env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
        build_env.update(UV_PYTHON_DOWNLOADS="never", UV_OFFLINE="1")
        run([shutil.which("uv") or "uv", "pip", "install", "--offline", "--no-deps", "--cache-dir", BASE / "uv-cache",
             "--python", python, wheel], cwd=work, env=build_env, log="install.log")
        # Installed commands are prohibited from spawning tools or opening any
        # network connection. This hook lives only in this disposable venv.
        (site / "sitecustomize.py").write_text(
            "import os,sys\nfrom pathlib import Path\n"
            "def guard(event,args):\n"
            " if event in ('subprocess.Popen','os.system','socket.connect','socket.getaddrinfo'):\n"
            "  Path(os.environ['PODGROVE_PROOF_GUARD']).write_text(event)\n"
            "  raise RuntimeError('External activity forbidden during installed package proof')\n"
            "sys.addaudithook(guard)\n")
        env = {key: value for key, value in build_env.items() if key not in
               ("PODGROVE_CONTEXT", "PODGROVE_NAMESPACE", "DOCKER_HOST", "DOCKER_CONTEXT")}
        env.update(PODGROVE_STATE_HOME=str(work / "absent-state"), PODGROVE_PROOF_GUARD=str(work / "forbidden.log"))
        cli = isolated / "bin/podgrove"
        output, _ = run([python, "-I", "-c", "import json,podgrove,podgrove.bootstrap;print(json.dumps([podgrove.__file__,podgrove.bootstrap.__file__]))"], cwd=app, env=env)
        imports = json.loads(output)
        assert all(Path(value).is_relative_to(isolated) and not Path(value).is_relative_to(REPO) for value in imports)
        report["installed_imports"] = imports
        for args in (["--help"], ["bootstrap", "--help"], ["web", "--help"], ["env", "--help"]):
            output, _ = run([cli, *args], cwd=app, env=env)
            assert "podgrove" in output
        report["checks"]["installed_help"] = True
        for mode in ("shared", "worktree"):
            base = "package-" + mode
            config = {"cluster": {"context": "package:offline", "namespace": base, "namespace_mode": mode,
                                  "storage_class": "package-delete-sc"},
                      "network": {"blocked_cidrs": ["8.8.8.0/24", "2001:db8:abcd::/48"]},
                      "sync": {"exclude": ["node_modules", ".venv"]},
                      "compose": {"files": ["missing-compose.yaml"], "env_file": "missing.env"}}
            (app / "podgrove.yml").write_text(yaml.safe_dump(config))
            target = work / (mode + "-bootstrap")
            args = [cli, "bootstrap", "--output", target]
            if mode == "worktree":
                args += ["--developer-group", "package-developers"]
            output, _ = run(args, cwd=app, env=env, log=f"installed-{mode}.log")
            files = sorted(target.iterdir())
            assert len(files) == 5
            assert all(file.suffix == ".yaml" for file in files)
            objects = [item for file in files for item in yaml.safe_load_all(file.read_text())]
            assert len(objects) == 9
            expected_identity = digest(str(app.resolve()).encode())[:12]
            expected_namespace = base if mode == "shared" else f"{base}-wt-{expected_identity}"
            marker, = [item for item in objects if item["kind"] == "ConfigMap"]
            assert marker["metadata"]["name"] == "podgrove-bootstrap"
            assert marker["data"]["namespace_mode"] == mode
            assert marker["data"].get("environment") == (None if mode == "shared" else expected_identity)
            assert all(item["metadata"]["namespace"] == expected_namespace for item in objects)
            assert all("podgrove.dev/environment" not in item["metadata"].get("labels", {}) for item in objects)
            assert {item["kind"] for item in objects} == {"ConfigMap", "NetworkPolicy", "ServiceAccount", "Role", "RoleBinding"}
            baseline, = list(yaml.safe_load_all((target / "05-network-isolation.yaml").read_text()))
            assert baseline["spec"] == {"podSelector": {"matchLabels": {"app.kubernetes.io/managed-by": "podgrove"}},
                                        "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []}
            for item in objects:
                assert item.get("roleRef", {}).get("kind", "Role") == "Role"
                for rule in item.get("rules", []):
                    assert not set(rule["resources"]) & {"namespaces", "nodes", "storageclasses", "persistentvolumes",
                                                         "clusterroles", "clusterrolebindings", "secrets"}
            assert f"kubectl --context package:offline --namespace {expected_namespace} apply -f" in output
            shutil.copytree(target, BASE / f"generated-{mode}")
            report["checks"][f"installed_{mode}_bootstrap"] = {"namespace": expected_namespace, "files": len(files),
                                                              "objects": len(objects), "only_namespaced_resources": True,
                                                              "managed_pod_deny_policy": True}
        smoke = app / "verify_package_installed.py"
        shutil.copy2(REPO / "scripts" / smoke.name, smoke)
        output, _ = run([python, "-I", smoke], cwd=app, env=env, log="installed-web.log")
        web = json.loads(output)
        assert web.pop("schema") == json.loads(snapshot["schema/podgrove-v1.schema.json"])
        assert web["assets"] == {name.rsplit("/", 1)[1]: digest(raw) for name, raw in snapshot.items()
                                 if name.startswith("podgrove/web_static/")}
        report["checks"]["installed_schema_and_mock_web"] = web
        (app / "podgrove.yml").write_text("cluster:\n  context: package:offline\n  storage_class: package-delete-sc\n")
        target = work / "must-not-exist"
        _, stderr = run([cli, "bootstrap", "--output", target], cwd=app, env=env, expected=1)
        assert "namespace" in stderr and not target.exists()
        report["checks"]["missing_namespace_refused_without_output"] = True
        assert not (work / "absent-state").exists()
        assert not (work / "forbidden.log").exists()
        report["checks"]["no_compose_cluster_network_git_or_runtime_state"] = True
    report["checks"]["isolated_temporary_environment_removed"] = not work.exists()
    assert source_snapshot() == snapshot, "Source changed during verification; preserve this candidate and rebuild"
    report["checks"]["final_source_parity"] = True
    parity = {"status": "passed", "source_files": len(snapshot), "sha256": report["source_sha256"],
              "checked_at": datetime.now(timezone.utc).isoformat()}
    (BASE / "final-source-parity.json").write_text(json.dumps(parity, indent=2) + "\n")
    report["status"] = "passed"


def deliver() -> dict:
    """Preserve only the matching old pair, then replace it with verified bytes."""
    assert {name: digest(raw) for name, raw in source_snapshot().items()} == report["source_sha256"], "Source changed before delivery"
    target = REPO / "dist"
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError("Refusing unsafe dist path")
    target.mkdir(exist_ok=True)
    prior = BASE / "prior-dist"
    prior.mkdir()
    result = {"status": "running", "prior": {}, "delivered": {}}
    for filename, info in report["archives"].items():
        origin, destination = DIST / filename, target / filename
        assert digest(origin.read_bytes()) == info["sha256"]
        if destination.exists() or destination.is_symlink():
            existing = destination.lstat()
            if not stat.S_ISREG(existing.st_mode) or destination.is_symlink():
                raise ValueError(f"Refusing non-regular prior archive: {destination}")
            raw = destination.read_bytes()
            (prior / filename).write_bytes(raw)
            result["prior"][filename] = {"sha256": digest(raw), "bytes": len(raw)}
    # All existing exact-name targets have been inspected and archived first.
    for filename, info in report["archives"].items():
        origin, destination = DIST / filename, target / filename
        if filename in result["prior"]:
            assert not destination.is_symlink() and digest(destination.read_bytes()) == result["prior"][filename]["sha256"]
        else:
            assert not destination.exists() and not destination.is_symlink()
        descriptor, temporary = tempfile.mkstemp(prefix=".podgrove-verified-", dir=target)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(origin.read_bytes())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        assert digest(destination.read_bytes()) == info["sha256"]
        result["delivered"][filename] = {"path": str(destination), **info}
    result["status"] = "passed"
    (BASE / "dist-delivery.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None) -> int:
    global BASE, DIST, report
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New evidence directory beneath an existing parent")
    parser.add_argument("--deliver", action="store_true", help="Archive prior matching dist files and deliver the verified pair")
    args = parser.parse_args(argv)
    try:
        BASE = prepare_output(args.output)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    DIST = BASE / "dist"
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "running", "checks": {},
              "scope": "Offline build and isolated installed-wheel checks; no Git, Kubernetes, Docker or network calls."}
    try:
        verify()
        if args.deliver:
            report["delivery"] = deliver()
    except BaseException as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        (BASE / "proof.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "proof": str(BASE / "proof.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
