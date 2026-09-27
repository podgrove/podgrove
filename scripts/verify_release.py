#!/usr/bin/env python3
"""Check release archives, install the exact wheel in a disposable venv, and seal assets.

This contacts PyPI only to install hash-locked runtime dependencies. CLI probes
run outside the checkout with an audit hook rejecting network and subprocesses.
It never reads kubeconfig, connects to Docker/Kubernetes, or installs globally.
"""
from __future__ import annotations

import argparse
import ast
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

from homebrew_formula import SHA256, render, runtime_packages, validated_version


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_version(root: Path) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    version = validated_version(project["version"])
    declarations = ast.parse((root / "podgrove/__init__.py").read_text()).body
    versions = [ast.literal_eval(node.value) for node in declarations if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
    lock = tomllib.loads((root / "uv.lock").read_text())
    locked = [item["version"] for item in lock["package"] if item["name"] == "podgrove"]
    manifest = json.loads((root / ".release-please-manifest.json").read_text())["."]
    if versions != [version] or locked != [version] or manifest != version:
        raise ValueError("Project, module, uv.lock, and release-please versions must agree")
    return version


def remove_build_marker(dist: Path) -> None:
    # uv creates this single-byte marker alongside every build. It is not a
    # release asset; refuse anything except the exact known ordinary file.
    marker = dist / ".gitignore"
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != b"*":
            raise ValueError("Unexpected build-directory .gitignore")
        marker.unlink()


def check_archives(root: Path, dist: Path, version: str) -> tuple[Path, Path]:
    sdist = dist / f"podgrove-{version}.tar.gz"
    wheel = dist / f"podgrove-{version}-py3-none-any.whl"
    if sorted(path.name for path in dist.iterdir()) != sorted([sdist.name, wheel.name]):
        raise ValueError("Expected exactly the one sdist and one wheel for this version in a fresh output directory")
    expected = {path.relative_to(root).as_posix(): path.read_bytes() for path in (root / "podgrove").rglob("*")
                if path.is_file() and (path.suffix == ".py" or "web_static" in path.parts)
                and "__pycache__" not in path.parts}
    prefix = f"podgrove-{version}/"
    with tarfile.open(sdist) as archive:
        names = set()
        for member in archive:
            relative = PurePosixPath(member.name)
            if (relative.is_absolute() or ".." in relative.parts or member.issym() or member.islnk()
                    or not (member.isfile() or member.isdir())):
                raise ValueError(f"Unsafe source archive member: {member.name}")
            if member.name in names:
                raise ValueError(f"Duplicate source archive member: {member.name}")
            names.add(member.name)
        forbidden = {".git", ".venv", "artifacts", "node_modules", "__pycache__"}
        if any(forbidden.intersection(PurePosixPath(name).parts) for name in names):
            raise ValueError("Local execution/private evidence must not be packaged")
        if prefix + "podgrove.yml" in names or prefix + ".env" in names or prefix + "HANDOFF.md" in names:
            raise ValueError("Private workspace configuration/handoff must not be packaged")
        package_members = {member.name.removeprefix(prefix) for member in archive.getmembers()
                           if member.isfile() and member.name.startswith(prefix + "podgrove/")}
        if package_members != set(expected):
            raise ValueError("Source archive package payload differs from the exact checkout file set")
        for name in ("README.md", "LICENSE", "pyproject.toml", "uv.lock"):
            stream = archive.extractfile(prefix + name)
            if stream is None or stream.read() != (root / name).read_bytes():
                raise ValueError(f"Source archive differs from checkout: {name}")
        for name, data in expected.items():
            stream = archive.extractfile(prefix + name)
            if stream is None or stream.read() != data:
                raise ValueError(f"Source archive differs from checkout: {name}")
    with zipfile.ZipFile(wheel) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("Duplicate wheel members")
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                raise ValueError(f"Unsafe wheel member: {member.filename}")
        if {name for name in archive.namelist() if name.startswith("podgrove/")} != set(expected):
            raise ValueError("Wheel package payload differs from the exact checkout file set")
        for name, data in expected.items():
            if archive.read(name) != data:
                raise ValueError(f"Wheel differs from checkout: {name}")
        headers = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(headers) != 1:
            raise ValueError("Wheel must contain exactly one package metadata file")
        metadata = BytesParser().parsebytes(archive.read(headers[0]))
        if metadata["Name"] != "podgrove" or metadata["Version"] != version:
            raise ValueError("Wheel metadata version/name mismatch")
        if not any(name.endswith("/licenses/LICENSE") for name in archive.namelist()):
            raise ValueError("Wheel must include the project license")
        if any(not name.startswith(("podgrove/", f"podgrove-{version}.dist-info/"))
               for name in archive.namelist()):
            raise ValueError("Unexpected top-level payload in wheel")
    return sdist, wheel


def locked_requirements(lock: dict, wheel: Path) -> str:
    lines = [f"{wheel.resolve().as_uri()} --hash=sha256:{sha256(wheel)}"]
    for package in runtime_packages(lock):
        hashes = {entry.get("hash", "") for entry in package.get("wheels", []) + [package.get("sdist", {})]}
        if not hashes or any(not value.startswith("sha256:") or not SHA256.fullmatch(value[7:]) for value in hashes):
            raise ValueError(f"Missing locked dependency hashes: {package['name']}")
        name, version = package["name"], package["version"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", version):
            raise ValueError("Unsafe locked dependency name/version")
        lines.append(f"{name}=={version} " + " ".join(f"--hash={value}" for value in sorted(hashes)))
    return "\n".join(lines) + "\n"


def run(arguments: list[str], *, cwd: Path, env: dict | None = None) -> str:
    result = subprocess.run(arguments, cwd=cwd, env=env, text=True, capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {arguments[0]}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def smoke_installed(wheel: Path, lock: dict, version: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="podgrove-release-") as temporary:
        work = Path(temporary).resolve()
        venv = work / "venv"
        run(["uv", "--no-config", "venv", "--python", sys.executable, str(venv)], cwd=work)
        requirements = work / "requirements.txt"
        requirements.write_text(locked_requirements(lock, wheel))
        python = venv / "bin/python"
        run(["uv", "--no-config", "pip", "install", "--python", str(python), "--no-deps", "--require-hashes",
             "-r", str(requirements)], cwd=work)
        run(["uv", "--no-config", "pip", "check", "--python", str(python)], cwd=work)
        site = Path(run([str(python), "-I", "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"],
                        cwd=work).strip())
        (site / "sitecustomize.py").write_text(
            "import sys\n"
            "def guard(event,args):\n"
            " if event in ('subprocess.Popen','os.system','socket.connect','socket.getaddrinfo'):\n"
            "  raise RuntimeError('External activity forbidden during release smoke test')\n"
            "sys.addaudithook(guard)\n")
        env = {key: value for key, value in os.environ.items() if key not in
               {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "KUBECONFIG", "PODGROVE_CONTEXT", "PODGROVE_NAMESPACE",
                "DOCKER_HOST", "DOCKER_CONTEXT"}}
        env.update(HOME=str(work), KUBECONFIG=str(work / "no-kubeconfig"),
                   PODGROVE_STATE_HOME=str(work / "state"))
        cli = str(venv / "bin/podgrove")
        if run([cli, "--version"], cwd=work, env=env).strip() != version:
            raise ValueError("Installed CLI version mismatch")
        for command in (["--help"], ["web", "--help"], ["bootstrap", "--help"]):
            run([cli, *command], cwd=work, env=env)
        imported = run([str(python), "-I", "-c", "import podgrove;print(podgrove.__file__)"], cwd=work, env=env)
        if not Path(imported.strip()).resolve().is_relative_to(venv):
            raise ValueError("Installed import escaped disposable venv")
        (work / "podgrove.yml").write_text(
            "version: 1\ncluster:\n  context: release:offline\n  namespace: podgrove-release-test\n")
        run([cli, "bootstrap", "--output", str(work / "manifests")], cwd=work, env=env)
        if not list((work / "manifests").glob("*.yaml")):
            raise ValueError("Installed bootstrap produced no manifests")
        assets = run([str(python), "-I", "-c",
                      "from importlib.resources import files; p=files('podgrove').joinpath('web_static');"
                      "assert p.joinpath('index.html').read_text();assert p.joinpath('app.js').read_text();"
                      "assert p.joinpath('style.css').read_text();assert p.joinpath('tokens.css').read_text();print('ok')"], cwd=work, env=env)
        if assets.strip() != "ok":
            raise ValueError("Installed dashboard assets missing")
    return {"isolated_import": True, "version": True, "cli_help": True, "bootstrap_offline": True,
            "dashboard_assets": True, "runtime_dependency_hashes": True, "pip_check": True}


def verify_bundle(dist: Path) -> dict:
    manifest = json.loads((dist / "release-manifest.json").read_text())
    version = validated_version(manifest["version"])
    if manifest["tag"] != "v" + version or not re.fullmatch(r"[a-f0-9]{40}", manifest["source_commit"]):
        raise ValueError("Invalid release identity")
    expected = {f"podgrove-{version}.tar.gz", f"podgrove-{version}-py3-none-any.whl", "podgrove.rb"}
    if set(manifest["assets"]) != expected:
        raise ValueError("Unexpected release assets")
    for name, digest in manifest["assets"].items():
        if not SHA256.fullmatch(digest) or sha256(dist / name) != digest:
            raise ValueError(f"Release asset hash mismatch: {name}")
    all_assets = sorted([*expected, "release-manifest.json"])
    checksums = "".join(f"{sha256(dist / name)}  {name}\n" for name in all_assets)
    if (dist / "SHA256SUMS").read_text() != checksums:
        raise ValueError("Checksum manifest mismatch")
    if sorted(path.name for path in dist.iterdir()) != sorted([*all_assets, "SHA256SUMS"]):
        raise ValueError("Unexpected file in sealed release directory")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--tag")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.source_sha):
        parser.error("--source-sha must be the exact 40-character source commit")
    root, dist = args.root.resolve(), args.dist.resolve()
    version = project_version(root)
    if args.tag and args.tag != "v" + version:
        parser.error("Tag disagrees with the source version")
    remove_build_marker(dist)
    sdist, wheel = check_archives(root, dist, version)
    lock = tomllib.loads((root / "uv.lock").read_text())
    checks = smoke_installed(wheel, lock, version)
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    (dist / "podgrove.rb").write_text(render(version=version, sdist_sha256=sha256(sdist), lock=lock,
                                            license_id=project.get("license", "")))
    assets = {path.name: sha256(path) for path in sorted(dist.iterdir())}
    manifest = {"version": version, "tag": "v" + version, "source_commit": args.source_sha,
                "assets": assets, "checks": checks}
    (dist / "release-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (dist / "SHA256SUMS").write_text("".join(f"{sha256(path)}  {path.name}\n" for path in sorted(dist.iterdir())))
    verify_bundle(dist)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
