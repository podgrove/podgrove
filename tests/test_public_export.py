"""Public exports retain reviewed bytes while excluding local state and history."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import prepare_public as public


def make_source(tmp_path, files=None):
    root = tmp_path / "source"
    root.mkdir()
    files = files or {"README.md": b"# A public project\n", "package/main.py": b"print('hello')\n"}
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest = root / public.DEFAULT_MANIFEST
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"version": 1, "files": sorted([*files, public.DEFAULT_MANIFEST])}) + "\n")
    return root


def change_manifest(root, change):
    target = root / public.DEFAULT_MANIFEST
    data = json.loads(target.read_text())
    change(data)
    target.write_text(json.dumps(data))


def test_export_has_exact_bytes_modes_and_deterministic_report_without_git_or_local_state(tmp_path):
    root = make_source(tmp_path)
    (root / "package/main.py").chmod(0o4755)
    for name in (".git/config", "artifacts/private.txt", ".venv/secrets.txt", "podgrove.yml", "extra.py"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("local only")
    report = tmp_path / "report.json"
    first = public.export(root, tmp_path / "public-a", report=report)
    second = public.export(root, tmp_path / "public-b")
    assert first == second == json.loads(report.read_text())
    expected = public.load_manifest(root)
    assert sorted(path.relative_to(tmp_path / "public-a").as_posix()
                  for path in (tmp_path / "public-a").rglob("*") if path.is_file()) == expected
    for item in first["files"]:
        target = tmp_path / "public-a" / item["path"]
        original = root / item["path"]
        assert target.read_bytes() == original.read_bytes()
        assert item["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
        assert item["bytes"] == target.stat().st_size
        assert item["mode"] == f"{target.stat().st_mode & 0o7777:04o}"
    assert (tmp_path / "public-a/package/main.py").stat().st_mode & 0o7777 == 0o755
    assert (tmp_path / "public-a/README.md").stat().st_mode & 0o7777 == 0o644
    assert str(root) not in report.read_text()
    assert first["file_count"] == len(expected)
    assert "not a comprehensive secret scanner" in public.__doc__


@pytest.mark.parametrize("path", [
    "/etc/passwd", "../outside", "a/../../outside", "a/./file", "a//file", "a/", "a\\file", "",
    ".git/config", "nested/.git/config", ".env", "podgrove.yml", "HANDOFF.md", ".venv/config",
    "artifacts/proof.json", "build/output", "thing.egg-info/PKG-INFO", "bad\nname", "bad\x00name",
])
def test_invalid_or_operational_paths_refuse_before_creating_output(tmp_path, path):
    root = make_source(tmp_path)
    change_manifest(root, lambda data: data["files"].append(path))
    with pytest.raises(public.ExportError):
        public.export(root, tmp_path / "public")
    assert not (tmp_path / "public").exists()


@pytest.mark.parametrize("change", [
    lambda data: data.update(version=True),
    lambda data: data.update(version=2),
    lambda data: data.update(extra="unknown"),
    lambda data: data.update(files=[]),
    lambda data: data.update(files="README.md"),
    lambda data: data["files"].reverse(),
    lambda data: data["files"].append(data["files"][0]),
    lambda data: data["files"].remove(public.DEFAULT_MANIFEST),
    lambda data: data["files"].append(3),
])
def test_invalid_manifest_refuses_closed(tmp_path, change):
    root = make_source(tmp_path)
    change_manifest(root, change)
    with pytest.raises(public.ExportError):
        public.export(root, tmp_path / "public")
    assert not (tmp_path / "public").exists()


@pytest.mark.parametrize("content", [b'{"version":1,"version":2,"files":[]}', b"not-json", b"[]"])
def test_manifest_must_be_unambiguous_json(tmp_path, content):
    root = make_source(tmp_path)
    (root / public.DEFAULT_MANIFEST).write_bytes(content)
    with pytest.raises(public.ExportError):
        public.export(root, tmp_path / "public")


def test_sensitive_manifest_filename_is_not_echoed_or_read(tmp_path):
    root = make_source(tmp_path)
    sensitive = "ghp" + "_" + "z" * 36
    change_manifest(root, lambda data: data.update(files=sorted([*data["files"], sensitive])))
    with pytest.raises(public.ExportError) as caught:
        public.export(root, tmp_path / "public")
    assert "publication/public-files.json:1: blocked credential pattern" in str(caught.value)
    assert sensitive not in str(caught.value)
    assert not (tmp_path / "public").exists()


@pytest.mark.parametrize("kind", ["source", "file", "parent", "manifest", "hardlink", "fifo"])
def test_no_symlink_hardlink_or_special_file_escape(tmp_path, kind):
    root = make_source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "main.py"
    secret.write_text("outside bytes must remain private")
    if kind == "source":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    elif kind == "parent":
        (root / "package/main.py").unlink()
        (root / "package").rmdir()
        (root / "package").symlink_to(outside, target_is_directory=True)
    elif kind == "manifest":
        target = root / public.DEFAULT_MANIFEST
        target.unlink()
        target.symlink_to(secret)
    else:
        target = root / "package/main.py"
        target.unlink()
        if kind == "file":
            target.symlink_to(secret)
        elif kind == "hardlink":
            os.link(secret, target)
        else:
            os.mkfifo(target)
    with pytest.raises(public.ExportError):
        public.export(root, tmp_path / "public")
    assert secret.read_text() == "outside bytes must remain private"
    assert not (tmp_path / "public").exists()


@pytest.mark.parametrize("existing", ["directory", "file", "symlink", "dangling"])
def test_existing_output_is_never_overwritten(tmp_path, existing):
    root = make_source(tmp_path)
    output = tmp_path / "public"
    outside = tmp_path / "preserved"
    outside.write_text("keep")
    if existing == "directory":
        output.mkdir()
        (output / "keep").write_text("keep")
    elif existing == "file":
        output.write_text("keep")
    else:
        output.symlink_to(outside if existing == "symlink" else tmp_path / "missing")
    with pytest.raises(public.ExportError, match="must not already exist"):
        public.export(root, output)
    assert outside.read_text() == "keep"
    if existing == "directory":
        assert (output / "keep").read_text() == "keep"


def test_output_and_report_must_stay_outside_source_and_each_other(tmp_path):
    root = make_source(tmp_path)
    with pytest.raises(public.ExportError, match="outside the source"):
        public.export(root, root / "public")
    with pytest.raises(public.ExportError, match="outside the source"):
        public.export(root, tmp_path / "public", report=root / "report.json")
    with pytest.raises(public.ExportError, match="outside the export"):
        public.export(root, tmp_path / "public", report=tmp_path / "public")
    assert not (tmp_path / "public").exists()


def test_report_refuses_existing_file_before_export(tmp_path):
    root = make_source(tmp_path)
    report = tmp_path / "report.json"
    report.write_text("original")
    with pytest.raises(public.ExportError, match="must not already exist"):
        public.export(root, tmp_path / "public", report=report)
    assert report.read_text() == "original"
    assert not (tmp_path / "public").exists()


def test_exclusive_output_creation_preserves_concurrent_creator(tmp_path, monkeypatch):
    root = make_source(tmp_path)
    output = tmp_path / "public"
    snapshot = public.snapshot_sources

    def create_after_validation(*args, **kwargs):
        result = snapshot(*args, **kwargs)
        output.mkdir()
        (output / "concurrent").write_text("keep")
        return result

    monkeypatch.setattr(public, "snapshot_sources", create_after_validation)
    with pytest.raises(public.ExportError, match="exclusively"):
        public.export(root, output)
    assert list(output.iterdir()) == [output / "concurrent"]
    assert (output / "concurrent").read_text() == "keep"


def test_exclusive_report_creation_preserves_concurrent_creator(tmp_path, monkeypatch):
    root = make_source(tmp_path)
    report = tmp_path / "report.json"
    snapshot = public.snapshot_sources

    def create_after_validation(*args, **kwargs):
        result = snapshot(*args, **kwargs)
        report.write_text("concurrent")
        return result

    monkeypatch.setattr(public, "snapshot_sources", create_after_validation)
    with pytest.raises(public.ExportError, match="exclusively"):
        public.export(root, tmp_path / "public", report=report)
    assert report.read_text() == "concurrent"
    assert not (tmp_path / "public").exists()


@pytest.mark.parametrize("content,fingerprint_input", [
    ("INTERNALCASE-42", "internalcase"),
    ("/Users/private-owner/project", "/users/private-owner"),
    ("owner:cluster-private-7", "owner:cluster-private-7"),
    ("private.example.test", "private.example.test"),
    ("InternalProduct", "internalproduct"),
])
def test_private_fingerprints_fail_without_publishing_actual_private_names(
        tmp_path, monkeypatch, content, fingerprint_input):
    monkeypatch.setattr(public, "PRIVATE_FINGERPRINTS", {
        hashlib.sha256(fingerprint_input.encode()).hexdigest(),
    })
    root = make_source(tmp_path, {"README.md": ("first line\n" + content + "\n").encode()})
    with pytest.raises(public.ExportError) as caught:
        public.export(root, tmp_path / "public")
    assert "README.md:2: blocked private identifier" in str(caught.value)
    assert content not in str(caught.value)
    assert not (tmp_path / "public").exists()


@pytest.mark.parametrize("content", [
    "ghp" + "_" + "x" * 36,
    "github" + "_pat_" + "x" * 70,
    "AK" + "IA" + "A" * 16,
    "xox" + "b-" + "0" * 24,
    "AI" + "za" + "A" * 35,
    "sk" + "-proj-" + "a" * 48,
    "-----BEGIN " + "PRIVATE KEY-----",
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
])
def test_recognizable_credentials_fail_without_echoing_value(tmp_path, content):
    root = make_source(tmp_path, {"README.md": ("first line\n" + content + "\n").encode()})
    with pytest.raises(public.ExportError) as caught:
        public.export(root, tmp_path / "public")
    assert "README.md:2: blocked" in str(caught.value)
    assert content not in str(caught.value)
    assert not (tmp_path / "public").exists()


@pytest.mark.parametrize("content", [b"bad-utf8-\xff", b"contains\x00nul"])
def test_binary_content_refuses_without_partial_output(tmp_path, content):
    root = make_source(tmp_path, {"README.md": content})
    with pytest.raises(public.ExportError):
        public.export(root, tmp_path / "public")
    assert not (tmp_path / "public").exists()


def test_missing_file_fails_before_output_and_report(tmp_path):
    root = make_source(tmp_path)
    (root / "README.md").unlink()
    with pytest.raises(public.ExportError, match="README.md"):
        public.export(root, tmp_path / "public", report=tmp_path / "report.json")
    assert not (tmp_path / "public").exists()
    assert not (tmp_path / "report.json").exists()


def test_total_size_is_bounded(tmp_path, monkeypatch):
    root = make_source(tmp_path)
    monkeypatch.setattr(public, "MAX_TOTAL_BYTES", 1)
    with pytest.raises(public.ExportError, match="total export size"):
        public.export(root, tmp_path / "public")


def test_current_public_manifest_and_guard_script_have_no_blocked_identifiers():
    root = Path(__file__).resolve().parents[1]
    files = public.load_manifest(root)
    assert public.DEFAULT_MANIFEST in files
    assert "scripts/prepare_public.py" in files
    assert "tests/test_public_export.py" in files
    for name in (public.DEFAULT_MANIFEST, "scripts/prepare_public.py", "tests/test_public_export.py"):
        public.check_content(name, (root / name).read_bytes())
