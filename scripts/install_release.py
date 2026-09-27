#!/usr/bin/env python3
"""Install a verified release into a new versioned venv and atomically select it.

Existing installations and running sessions are never changed. The bundle must
come from the trusted release; checksums establish byte integrity, not authorship.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import tomllib
import uuid

from verify_release import locked_requirements, run, sha256, verify_bundle

CHECKS = {"isolated_import", "version", "cli_help", "bootstrap_offline",
          "dashboard_assets", "runtime_dependency_hashes", "pip_check"}


def snapshot_bundle(source: Path, destination: Path, tag: str, commit: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Expected the release's exact 40-character source commit")
    for path in source.iterdir():
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Release bundle entries must be regular files, never symlinks")
        shutil.copyfile(path, destination / path.name)
    manifest = verify_bundle(destination)
    if manifest["tag"] != tag or manifest["source_commit"] != commit:
        raise ValueError("Release bundle does not match the requested tag and source commit")
    if any(manifest.get("checks", {}).get(name) is not True for name in CHECKS):
        raise ValueError("Release bundle lacks successful installed-package verification")
    return manifest


def bundle_lock(bundle: Path, version: str) -> dict:
    name = f"podgrove-{version}/uv.lock"
    with tarfile.open(bundle / f"podgrove-{version}.tar.gz") as archive:
        members = [item for item in archive if item.name == name]
        if len(members) != 1 or not members[0].isfile() or members[0].size > 8 * 1024 * 1024:
            raise ValueError("Release sdist must contain one regular, bounded uv.lock")
        stream = archive.extractfile(members[0])
        if stream is None:
            raise ValueError("Release dependency lock is unreadable")
        lock = tomllib.loads(stream.read().decode())
    roots = [item for item in lock.get("package", []) if item.get("name") == "podgrove"]
    if len(roots) != 1 or roots[0].get("version") != version:
        raise ValueError("Release dependency lock version disagrees with bundle")
    return lock


def isolated_environment(work: Path) -> dict:
    # No ambient package index, Python path, kubeconfig, or application settings.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("UV_", "PIP_", "PYTHON", "PODGROVE_", "DOCKER_"))
           and key not in {"VIRTUAL_ENV", "KUBECONFIG"}}
    env.update(HOME=str(work), KUBECONFIG=str(work / "absent-kubeconfig"),
               PODGROVE_STATE_HOME=str(work / "state"))
    return env


def smoke(venv: Path, version: str, work: Path, env: dict) -> None:
    python, cli = venv / "bin/python", venv / "bin/podgrove"
    if run([str(cli), "--version"], cwd=work, env=env).strip() != version:
        raise ValueError("Installed executable reports another version")
    probe = """
import importlib.metadata, importlib.resources, json, pathlib, runpy, sys
def guard(event, args):
    if event in ('subprocess.Popen', 'os.system', 'socket.connect', 'socket.getaddrinfo'):
        raise RuntimeError('External activity forbidden during installed release checks')
sys.addaudithook(guard)
import podgrove
assert pathlib.Path(podgrove.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
direct = json.loads(importlib.metadata.distribution('podgrove').read_text('direct_url.json') or '{}')
assert not direct.get('dir_info', {}).get('editable', False)
assets = importlib.resources.files('podgrove').joinpath('web_static')
for name in ('index.html', 'app.js', 'style.css', 'tokens.css', 'favicon.svg'):
    assert assets.joinpath(name).read_bytes()
sys.argv = ['podgrove', *sys.argv[1:]]
runpy.run_module('podgrove', run_name='__main__')
"""
    for arguments in (["--help"], ["web", "--help"], ["bootstrap", "--help"]):
        run([str(python), "-I", "-B", "-c", probe, *arguments], cwd=work, env=env)
    (work / "podgrove.yml").write_text(
        "version: 1\ncluster:\n  context: install:offline\n  namespace: podgrove-install-test\n")
    output = work / "manifests"
    run([str(python), "-I", "-B", "-c", probe, "bootstrap", "--output", str(output)], cwd=work, env=env)
    if not list(output.glob("*.yaml")):
        raise ValueError("Installed offline bootstrap produced no manifests")


def check_current(current: Path) -> None:
    if current.is_symlink():
        target = current.resolve()
        if target.parent.parent != current.parent or target.name != "venv" or not target.is_dir():
            raise ValueError("Existing current symlink does not select a versioned venv in this install root")
    elif current.exists():
        raise ValueError("Refusing to replace a non-symlink current path")


def install(dist: Path, root: Path, *, tag: str, commit: str, python: str = sys.executable,
            offline: bool = False) -> dict:
    with tempfile.TemporaryDirectory(prefix="podgrove-install-") as temporary:
        work = Path(temporary).resolve()
        bundle = work / "bundle"
        bundle.mkdir()
        manifest = snapshot_bundle(dist, bundle, tag, commit)
        version = manifest["version"]
        wheel = bundle / f"podgrove-{version}-py3-none-any.whl"
        requirements = work / "requirements.txt"
        requirements.write_text(locked_requirements(bundle_lock(bundle, version), wheel))
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(root / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = root / "current"
            check_current(current)
            destination = root / f"{version}-{commit[:12]}"
            # Exclusive creation also refuses dangling symlinks and partial installs.
            destination.mkdir(mode=0o700)
            venv = destination / "venv"
            link = root / f".current-{uuid.uuid4().hex}"
            try:
                env = isolated_environment(work)
                cache = Path(run(["uv", "--no-config", "cache", "dir"], cwd=work).strip())
                if not cache.is_absolute():
                    raise ValueError("uv returned an invalid cache directory")
                env["UV_CACHE_DIR"] = str(cache)
                options = ["--offline"] if offline else []
                run(["uv", "--no-config", *options, "venv", "--python", python, str(venv)], cwd=work, env=env)
                run(["uv", "--no-config", *options, "pip", "install", "--python", str(venv / "bin/python"),
                     "--index-url", "https://pypi.org/simple", "--no-deps", "--require-hashes",
                     "-r", str(requirements)], cwd=work, env=env)
                run(["uv", "--no-config", "pip", "check", "--python", str(venv / "bin/python")], cwd=work, env=env)
                smoke(venv, version, work, env)
                receipt = {"version": version, "tag": tag, "source_commit": commit,
                           "bundle_sha256": sha256(bundle / "SHA256SUMS"), "assets": manifest["assets"],
                           "executable": str(venv / "bin/podgrove"), "stable_executable": str(current / "bin/podgrove")}
                (destination / "installation.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
                check_current(current)
                link.symlink_to(venv)
                os.replace(link, current)
                return receipt
            except BaseException:
                # Only this invocation's exclusively created version is removed.
                # If interrupted immediately after atomic activation, preserve the
                # already verified target rather than leave a dangling current.
                if link.is_symlink():
                    link.unlink()
                if not (current.is_symlink() and current.resolve() == venv):
                    shutil.rmtree(destination)
                raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True, help="Directory containing all five release assets")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--install-root", type=Path, default=Path.home() / ".local/share/podgrove")
    parser.add_argument("--python", default=sys.executable, help="Python 3.11+ interpreter for the new venv")
    parser.add_argument("--offline", action="store_true", help="Use only dependencies already in uv's cache")
    args = parser.parse_args()
    try:
        result = install(args.dist.resolve(), args.install_root, tag=args.tag, commit=args.source_sha,
                         python=args.python, offline=args.offline)
    except (OSError, ValueError, RuntimeError, tarfile.TarError) as error:
        parser.exit(1, f"Installation refused: {error}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
