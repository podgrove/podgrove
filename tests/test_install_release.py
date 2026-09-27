"""Offline regressions for immutable release installation and atomic selection."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
from urllib.parse import unquote, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_installer():
    for name in ("homebrew_formula", "verify_release", "install_release"):
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
    return sys.modules["install_release"]


installer = load_installer()
COMMIT = "b" * 40
VERSION = "0.2.0"


def reseal_manifest(bundle):
    (bundle / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(bundle.iterdir()) if path.name != "SHA256SUMS"))


def seal(bundle):
    assets = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(bundle.iterdir())
              if path.name not in {"release-manifest.json", "SHA256SUMS"}}
    manifest = {"version": VERSION, "tag": "v" + VERSION, "source_commit": COMMIT,
                "assets": assets, "checks": {name: True for name in installer.CHECKS}}
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    reseal_manifest(bundle)
    return manifest


def write_lock(bundle, *, version=VERSION, kind="regular"):
    content = f'[[package]]\nname="podgrove"\nversion="{version}"\nsource={{editable="."}}\n'.encode()
    with tarfile.open(bundle / f"podgrove-{VERSION}.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo(f"podgrove-{VERSION}/uv.lock")
        member.size = len(content)
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/untrusted/uv.lock"
        archive.addfile(member, io.BytesIO(content))
        if kind == "duplicate":
            archive.addfile(member, io.BytesIO(content))


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "bundle"
    path.mkdir()
    write_lock(path)
    (path / f"podgrove-{VERSION}-py3-none-any.whl").write_bytes(b"fixture wheel bytes")
    (path / "podgrove.rb").write_text("fixture formula")
    seal(path)
    return path


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "installed"
    old = root / "0.1.0-aaaaaaaaaaaa" / "venv"
    old.mkdir(parents=True)
    (old / "preserve").write_text("existing runtime")
    (root / "current").symlink_to(old)
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments == ["uv", "--no-config", "cache", "dir"]:
            return str(tmp_path / "cache")
        if "venv" in arguments:
            (Path(arguments[-1]) / "bin").mkdir(parents=True)
        if "--require-hashes" in arguments:
            requirements = Path(arguments[-1]).read_text()
            assert "--hash=sha256:" in requirements
            assert "--no-deps" in arguments and "-e" not in arguments
        return ""

    monkeypatch.setattr(installer, "run", run)
    monkeypatch.setattr(installer, "smoke", lambda *args: None)
    return root, old, calls


def install(bundle, root, **kwargs):
    return installer.install(bundle, root, tag="v" + VERSION, commit=COMMIT, **kwargs)


def test_installs_new_version_and_atomically_selects_after_smoke(bundle, installation, monkeypatch):
    root, old, calls = installation
    checks = []

    def smoke(venv, version, work, env):
        assert (root / "current").resolve() == old
        assert venv == root / f"{VERSION}-{COMMIT[:12]}" / "venv"
        assert version == VERSION
        assert env["KUBECONFIG"].endswith("absent-kubeconfig")
        checks.append(venv)

    monkeypatch.setattr(installer, "smoke", smoke)
    receipt = install(bundle, root)
    assert checks == [(root / "current").resolve()]
    assert Path(receipt["executable"]).parent.parent == checks[0]
    assert (old / "preserve").read_text() == "existing runtime"
    assert receipt["source_commit"] == COMMIT
    saved = json.loads((checks[0].parent / "installation.json").read_text())
    assert saved == receipt
    assert not list(root.glob(".current-*"))
    assert any("check" in command for command, _ in calls)


@pytest.mark.parametrize("failure", ["venv", "install", "check", "smoke"])
def test_failure_preserves_previous_runtime_and_removes_only_new_partial(bundle, installation, monkeypatch, failure):
    root, old, _ = installation
    original = installer.run

    def run(arguments, **kwargs):
        if failure in arguments:
            raise RuntimeError("fixture failure")
        return original(arguments, **kwargs)

    monkeypatch.setattr(installer, "run", run)
    if failure == "smoke":
        monkeypatch.setattr(installer, "smoke", lambda *args: (_ for _ in ()).throw(RuntimeError("fixture failure")))
    with pytest.raises(RuntimeError, match="fixture failure"):
        install(bundle, root)
    assert (root / "current").resolve() == old
    assert (old / "preserve").read_text() == "existing runtime"
    assert not (root / f"{VERSION}-{COMMIT[:12]}").exists()


@pytest.mark.parametrize("kind", ["populated", "empty", "dangling-symlink"])
def test_existing_version_is_never_overwritten(bundle, installation, kind):
    root, old, calls = installation
    existing = root / f"{VERSION}-{COMMIT[:12]}"
    if kind == "dangling-symlink":
        existing.symlink_to(root / "missing")
    else:
        existing.mkdir()
        if kind == "populated":
            (existing / "preserve").write_text("conflicting payload")
    with pytest.raises(FileExistsError):
        install(bundle, root)
    assert not calls
    assert existing.exists() or existing.is_symlink()
    assert (root / "current").resolve() == old


@pytest.mark.parametrize("kind", ["file", "directory", "foreign-symlink"])
def test_current_conflicts_are_refused_without_installing(bundle, installation, kind, tmp_path):
    root, _, calls = installation
    current = root / "current"
    current.unlink()
    if kind == "file":
        current.write_text("keep")
    elif kind == "directory":
        current.mkdir()
    else:
        current.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="current"):
        install(bundle, root)
    assert not calls


@pytest.mark.parametrize("kind", ["wheel", "checksum", "identity", "checks", "symlink"])
def test_bad_bundle_refused_before_touching_install_root(bundle, tmp_path, kind):
    if kind == "wheel":
        (bundle / f"podgrove-{VERSION}-py3-none-any.whl").write_bytes(b"tampered")
    elif kind == "checksum":
        (bundle / "SHA256SUMS").write_text("wrong")
    elif kind in {"identity", "checks"}:
        path = bundle / "release-manifest.json"
        data = json.loads(path.read_text())
        if kind == "identity":
            data["source_commit"] = "c" * 40
        else:
            data["checks"]["pip_check"] = False
        path.write_text(json.dumps(data))
        reseal_manifest(bundle)
    else:
        (bundle / "foreign").symlink_to(bundle / "podgrove.rb")
    root = tmp_path / "not-created"
    with pytest.raises(ValueError):
        install(bundle, root)
    assert not root.exists()


@pytest.mark.parametrize("kind", ["symlink", "duplicate", "version"])
def test_sdist_lock_refuses_links_duplicates_and_version_mismatch(bundle, tmp_path, kind):
    write_lock(bundle, version="9.9.9" if kind == "version" else VERSION, kind=kind)
    seal(bundle)
    root = tmp_path / "not-created"
    with pytest.raises(ValueError, match="lock"):
        install(bundle, root)
    assert not root.exists()


def test_offline_flag_and_package_environment_are_constrained(bundle, installation, monkeypatch):
    root, _, calls = installation
    for key in ("PIP_INDEX_URL", "UV_INDEX", "PYTHONPATH", "DOCKER_HOST", "PODGROVE_CONFIG"):
        monkeypatch.setenv(key, "untrusted-fixture")
    install(bundle, root, offline=True)
    command, kwargs = next((command, kwargs) for command, kwargs in calls if "install" in command)
    assert "--offline" in command
    assert command[command.index("--index-url") + 1] == "https://pypi.org/simple"
    assert not any(value == "untrusted-fixture" for value in kwargs["env"].values())


def test_later_source_mutation_cannot_change_the_installed_snapshot(bundle, installation, monkeypatch):
    root, _, _ = installation
    original = installer.run

    def run(arguments, **kwargs):
        if "venv" in arguments:
            (bundle / f"podgrove-{VERSION}-py3-none-any.whl").write_bytes(b"changed after snapshot")
        if "--require-hashes" in arguments:
            line = Path(arguments[-1]).read_text().splitlines()[0]
            snapshot = Path(unquote(urlparse(line.split()[0]).path))
            assert snapshot.read_bytes() == b"fixture wheel bytes"
        return original(arguments, **kwargs)

    monkeypatch.setattr(installer, "run", run)
    install(bundle, root)


def test_interruption_after_atomic_activation_never_deletes_selected_runtime(bundle, installation, monkeypatch):
    root, old, _ = installation
    replace = installer.os.replace

    def interrupt_after_replace(source, target):
        replace(source, target)
        raise KeyboardInterrupt

    monkeypatch.setattr(installer.os, "replace", interrupt_after_replace)
    with pytest.raises(KeyboardInterrupt):
        install(bundle, root)
    assert (root / "current").resolve().is_dir()
    assert (root / "current").resolve().parent.joinpath("installation.json").is_file()
    assert (old / "preserve").read_text() == "existing runtime"
