#!/usr/bin/env python3
"""Copy an explicitly reviewed public source tree without Git or local state.

The content guard detects known private identifiers and recognizable credential
shapes. It is an additional check, not a comprehensive secret scanner or a
substitute for reviewing the export. No content is rewritten or uploaded.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

DEFAULT_MANIFEST = "publication/public-files.json"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 2048
FORBIDDEN_PARTS = frozenset({
    ".git", ".venv", "venv", ".env", ".envrc", "artifacts", "__pycache__",
    "node_modules", ".hallmark", ".pytest_cache", ".ruff_cache", "dist", "build",
})
# Store only fingerprints of reviewed internal identifiers. The public guard
# must not publish the private account/project names it is intended to catch.
PRIVATE_FINGERPRINTS = frozenset({
    "bd1726ba2c51797a7c62d15e5232041cc73ed48a777f73c97e39176955939261",
    "eb7be93d27fb30ce8be1a0936f77def9170aed570a17568c32e688c062330d7c",
    "5df93fd75a8372683141e67be15de2f755081d604977aa87791ce4b46ec61734",
    "a662b62952bd12ef2fc0af343ccebf9853b7716215961434bd39b906785161f6",
    "780afcc79eab7bfd82f26c755a609b23c4ed3af2f991fca89caa10c6524a99bd",
    "b738fdfccb93d1aa67e9d7f78e621dff8feab4b9e3b9b79eb25c4a0f4f764e08",
    "b9bf0eaf0ad6afe8e9850f72d032ff5eba165b6cc49ed58660a4876ee1169c73",
    "58d8cea39e3525a4eba67979a2ce06d988624ac52953eecb87f4263f5e24c2ff",
})
PRIVATE_CANDIDATES = (
    re.compile(r"/Users/[A-Za-z0-9_.-]+", re.I),
    re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_.:][A-Za-z0-9]+)+"),
    re.compile(r"[A-Za-z][A-Za-z0-9]*"),
)

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b"),
)


class ExportError(ValueError):
    """An export validation failure; messages never include source values."""


@dataclass(frozen=True)
class SourceFile:
    path: str
    content: bytes
    mode: int


def relative_file(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ExportError("Manifest paths must be canonical relative POSIX filenames")
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or any(part in {".", ".."} for part in path.parts)
            or any(part in FORBIDDEN_PARTS or part.endswith(".egg-info") for part in path.parts)
            or value in {"podgrove.yml", "HANDOFF.md"} or any(ord(char) < 32 for char in value)):
        raise ExportError("Manifest contains a forbidden or noncanonical path")
    return value


def read_regular(root: Path, relative: str, *, limit: int = MAX_FILE_BYTES) -> tuple[bytes, int]:
    """Walk from an open root descriptor; symlinks cannot redirect any segment."""
    relative = relative_file(relative)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, directory_flags)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=descriptor)
        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ExportError(f"{relative}: expected an ordinary, non-hard-linked source file")
            if metadata.st_size > limit:
                raise ExportError(f"{relative}: source file exceeds the export size limit")
            with os.fdopen(file_descriptor, "rb", closefd=False) as stream:
                content = stream.read(limit + 1)
            if len(content) > limit:
                raise ExportError(f"{relative}: source file exceeds the export size limit")
            return content, 0o755 if metadata.st_mode & 0o111 else 0o644
        finally:
            os.close(file_descriptor)
    except OSError as error:
        raise ExportError(f"{relative}: source must exist without symlinks or path escapes") from error
    finally:
        os.close(descriptor)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExportError("Manifest contains a duplicate JSON key")
        result[key] = value
    return result


def load_manifest(root: Path, manifest: str = DEFAULT_MANIFEST) -> list[str]:
    raw, _ = read_regular(root, manifest, limit=MAX_MANIFEST_BYTES)
    check_content(manifest, raw)
    try:
        data = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError) as error:
        raise ExportError("Public manifest must contain valid, unambiguous JSON") from error
    if (not isinstance(data, dict) or set(data) != {"version", "files"}
            or type(data["version"]) is not int or data["version"] != 1
            or not isinstance(data["files"], list) or not 1 <= len(data["files"]) <= MAX_FILES):
        raise ExportError("Public manifest requires version 1 and a nonempty files list")
    files = [relative_file(value) for value in data["files"]]
    if files != sorted(set(files)):
        raise ExportError("Public manifest files must be sorted and unique")
    if manifest not in files:
        raise ExportError("Public manifest must include itself in its reviewed files")
    return files


def check_content(path: str, content: bytes) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportError(f"{path}: public source must be UTF-8 text") from error
    if "\x00" in text:
        raise ExportError(f"{path}: binary source is not supported by this public manifest")
    for pattern in PRIVATE_CANDIDATES:
        for match in pattern.finditer(text):
            fingerprint = hashlib.sha256(match.group().casefold().encode()).hexdigest()
            if fingerprint in PRIVATE_FINGERPRINTS:
                line = text.count("\n", 0, match.start()) + 1
                raise ExportError(f"{path}:{line}: blocked private identifier; review the source without publishing it")
    for pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            raise ExportError(f"{path}:{line}: blocked credential pattern; review the source without publishing it")



def snapshot_sources(root: Path, manifest: str = DEFAULT_MANIFEST) -> list[SourceFile]:
    files = []
    total = 0
    for relative in load_manifest(root, manifest):
        content, mode = read_regular(root, relative)
        check_content(relative, content)
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ExportError("Public sources exceed the total export size limit")
        files.append(SourceFile(relative, content, mode))
    return files


def _new_destination(value: Path, root: Path, *, label: str) -> Path:
    path = Path(os.path.abspath(value.expanduser()))
    if path.is_symlink() or path.exists():
        raise ExportError(f"{label} must not already exist")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise ExportError(f"{label} parent must already exist") from error
    if not parent.is_dir():
        raise ExportError(f"{label} parent must be a directory")
    path = parent / path.name
    if path == root or path.is_relative_to(root) or root.is_relative_to(path):
        raise ExportError(f"{label} must be outside the source tree")
    return path


def export(source: Path, output: Path, *, report: Path | None = None,
           manifest: str = DEFAULT_MANIFEST) -> dict:
    if source.is_symlink():
        raise ExportError("Source directory must not be a symlink")
    try:
        root = source.resolve(strict=True)
    except OSError as error:
        raise ExportError("Source directory must exist") from error
    if not root.is_dir():
        raise ExportError("Source directory must be a directory")
    destination = _new_destination(output, root, label="Export directory")
    report_path = _new_destination(report, root, label="Report file") if report is not None else None
    if report_path is not None and (report_path == destination or report_path.is_relative_to(destination)
                                    or destination.is_relative_to(report_path)):
        raise ExportError("Report must be outside the export directory")
    sources = snapshot_sources(root, manifest)
    result = {
        "version": 1,
        "files": [{"path": item.path, "sha256": hashlib.sha256(item.content).hexdigest(),
                   "bytes": len(item.content), "mode": f"{item.mode:04o}"} for item in sources],
        "file_count": len(sources),
        "total_bytes": sum(len(item.content) for item in sources),
        "guard": "Known-pattern checks only; independent secret scanning and source review are required.",
    }
    report_descriptor = None
    # All source validation precedes destination creation. Exclusive opens also
    # preserve files/directories created concurrently after the earlier check.
    if report_path is not None:
        try:
            report_descriptor = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise ExportError("Report file could not be created exclusively") from error
    try:
        try:
            destination.mkdir(mode=0o700)
        except OSError as error:
            raise ExportError("Export directory could not be created exclusively") from error
        for item in sources:
            target = destination / item.path
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, item.mode)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(item.content)
                os.fchmod(stream.fileno(), item.mode)
        destination.chmod(0o755)
        if report_descriptor is not None:
            with os.fdopen(report_descriptor, "w", encoding="utf-8", closefd=False) as stream:
                json.dump(result, stream, sort_keys=True, indent=2)
                stream.write("\n")
    finally:
        if report_descriptor is not None:
            os.close(report_descriptor)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True, help="New export directory outside the source tree")
    parser.add_argument("--report", type=Path, help="New JSON report outside both source and export trees")
    args = parser.parse_args()
    try:
        result = export(args.source, args.output, report=args.report)
    except (ExportError, OSError) as error:
        print(f"Public export refused: {error}", file=sys.stderr)
        return 1
    print(f"Prepared {result['file_count']} reviewed files ({result['total_bytes']} bytes). No Git history copied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
