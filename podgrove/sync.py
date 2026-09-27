"""Incremental worktree mirrors for a dedicated remote Docker engine.

The helper binds the daemon's absolute worktree path, so Compose sees the same
paths it resolved locally. Transfer archives are streamed from temporary files;
files are copied into their existing inode to keep single-file bind mounts live.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from typing import BinaryIO

from .errors import PodgroveError
from .sync_filter import excluded, validate_patterns
from .sync_transport import RECEIVER, TarStream

_IMAGE = "alpine:3.21"
_ID_LABEL = "io.podgrove.sync.identity"
_ROOT_LABEL = "io.podgrove.sync.root"
_STAGE = "/tmp/podgrove-transfer"


class SnapshotRace(PodgroveError):
    """The local source changed before any batch was sent; a fresh read is safe."""


LOCAL_RETRY_DELAYS = (0.05, 0.15)

# Every user-controlled path arrives through an argv entry or a NUL-delimited
# control file, never through interpolation into a shell program.
_INITIALIZE = r'''
set -eu
if [ ! -f /metadata/initialized ]; then
    if [ -n "$(find /workspace -mindepth 1 -maxdepth 1 -exec echo occupied \; -quit)" ]; then
        echo 'Refusing to overwrite a nonempty daemon directory without podgrove ownership' >&2
        exit 1
    fi
    : > /metadata/initialized
fi
if [ -z "$(find /workspace -mindepth 1 -maxdepth 1 -exec echo occupied \; -quit)" ]; then
    echo empty
fi
'''
_RECEIVE = r'''
set -eu
incoming=$(mktemp -d /tmp/podgrove-incoming.XXXXXX)
trap 'rm -rf "$incoming" /tmp/podgrove-transfer' EXIT
tar -xpf - -C "$incoming"
rm -rf /tmp/podgrove-transfer
mv "$incoming/podgrove-transfer" /tmp/podgrove-transfer
rmdir "$incoming"
'''
_APPLY = r'''
set -eu
stage=/tmp/podgrove-transfer
trap 'rm -rf /tmp/podgrove-transfer' EXIT
fail() { echo "$*" >&2; exit 1; }
check_path() {
    rel=$1
    case "$rel" in
        ''|/*|..|../*|*/../*|*/..|.git|.git/*|*/.git|*/.git/*)
            fail "Unsafe mirror path: $rel" ;;
    esac
    target=/workspace/$rel
    cursor=$target
    while [ "$cursor" != /workspace ]; do
        [ ! -L "$cursor" ] || fail "Remote symlink blocks sync: $rel"
        cursor=${cursor%/*}
    done
}
while IFS= read -r -d '' rel; do
    check_path "$rel"
    if [ -d "$target" ]; then
        # A container may have written its own data below this directory. Only
        # remove directories that became empty after deleting tracked entries.
        rmdir "$target" 2>/dev/null || true
    elif [ -e "$target" ]; then
        rm -f "$target"
    fi
done < "$stage/control/deleted"
while IFS= read -r -d '' rel; do
    check_path "$rel"
    [ ! -e "$target" ] || [ -d "$target" ] || fail "Remote non-directory blocks sync: $rel"
    mkdir -p "$target"
done < "$stage/control/directories"
while IFS= read -r -d '' rel; do
    check_path "$rel"
    source=$stage/payload/$rel
    [ ! -e "$target" ] || [ -f "$target" ] || fail "Remote non-file blocks sync: $rel"
    # Redirection truncates the original inode; mv/cp extraction would leave an
    # existing single-file Compose bind mount attached to stale file contents.
    cat "$source" > "$target"
    chown "$(stat -c %u:%g "$source")" "$target"
    chmod "$(stat -c %a "$source")" "$target"
    touch -r "$source" "$target"
done < "$stage/control/files"
while IFS= read -r -d '' rel; do
    check_path "$rel"
    chown "$(stat -c %u:%g "$stage/payload/$rel")" "$target"
    chmod "$(stat -c %a "$stage/payload/$rel")" "$target"
done < "$stage/control/directory_modes"
cp "$stage/control/baseline.json" /metadata/baseline.json.tmp
mv /metadata/baseline.json.tmp /metadata/baseline.json
'''


class Synchronizer:
    """Mirror only requested worktree files into an owned remote Docker root."""

    def __init__(self, root: Path, paths: list[Path], docker_env: dict[str, str], identity: str,
                 *, exclude: list[str] | None = None):
        self.root = root.resolve()
        self.exclude = validate_patterns(list(exclude or []))
        self.paths: list[Path] = []
        self.env = dict(docker_env)
        # Resolve using the caller's actual PATH even if its Docker environment
        # deliberately only contains connection variables.
        self.docker = shutil.which("docker", path=self.env.get("PATH")) or "docker"
        suffix = hashlib.sha256(identity.encode()).hexdigest()[:20]
        self.container = f"podgrove-sync-{suffix}"
        self.volume = f"{self.container}-state"
        self.labels = {_ID_LABEL: identity, _ROOT_LABEL: hashlib.sha256(str(self.root).encode()).hexdigest()}
        self._baseline: dict[str, dict] = {}
        self._started = False
        self._created = False
        self.activity_callback = None
        self.last_timing = {}
        self._cancelled = threading.Event()
        self._process_lock = threading.Lock()
        self._process = None
        self._receiver = None
        self._validate_root()
        for source in paths:
            path = Path(os.path.abspath(source if source.is_absolute() else self.root / source))
            self._validate_source(path, require_exists=True)
            if path not in self.paths:
                self.paths.append(path)
        # Avoid walking the same subtree twice when directory/file binds overlap.
        self.paths = [
            path for path in sorted(self.paths)
            if not any(other != path and other in path.parents for other in self.paths)
        ]
        # Do the recursive validation before creating anything on the daemon.
        self._retry_local(lambda: self._snapshot(allow_missing=False))

    def _validate_root(self) -> None:
        if not self.root.is_dir():
            raise PodgroveError(f"Sync worktree root is not a directory: {self.root}")
        if self.root == Path("/") or len(self.root.parts) < 3:
            raise PodgroveError(f"Refusing unsafe daemon mirror root: {self.root}")
        forbidden = ("/proc", "/sys", "/dev", "/etc", "/bin", "/sbin", "/usr", "/lib", "/boot",
                     "/run", "/var/lib/docker", "/var/run", "/private/etc", "/private/var/run")
        if any(self.root == Path(p) or Path(p) in self.root.parents for p in forbidden):
            raise PodgroveError(f"Refusing system directory as daemon mirror root: {self.root}")
        if ":" in str(self.root) or "\n" in str(self.root):
            raise PodgroveError(f"Sync worktree root cannot contain ':' or a newline: {self.root}")

    def _validate_source(self, path: Path, *, require_exists: bool) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise PodgroveError(f"Sync source is outside worktree root: {path}") from exc
        if ".git" in relative.parts:
            raise PodgroveError(f"Sync source cannot contain .git: {path}")
        if excluded(relative.as_posix(), self.exclude):
            raise PodgroveError(f"Explicit sync source matches sync.exclude: {path}")
        cursor = self.root
        for component in relative.parts:
            cursor /= component
            if cursor.is_symlink():
                raise PodgroveError(f"Symlinks are unsupported sync sources: {cursor}")
        if require_exists and not path.exists():
            raise PodgroveError(f"Sync source does not exist: {path}")

    @staticmethod
    def _fingerprint(info: os.stat_result) -> dict:
        return {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
                "inode": info.st_ino, "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}

    def _open_file(self, path: Path) -> int:
        """Open beneath the root without following any source path symlinks."""
        relative = path.relative_to(self.root)
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
        parent = os.open(self.root, directory_flags)
        try:
            for component in relative.parts[:-1]:
                child = os.open(component, directory_flags, dir_fd=parent)
                os.close(parent)
                parent = child
            return os.open(relative.name, flags, dir_fd=parent)
        finally:
            os.close(parent)

    def _file_entry(self, path: Path, info: os.stat_result, old: dict | None) -> dict:
        fingerprint = self._fingerprint(info)
        if old and old.get("kind") == "file" and all(old.get(k) == v for k, v in fingerprint.items()):
            return dict(old)
        digest = hashlib.sha256()
        try:
            fd = self._open_file(path)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise PodgroveError(f"Only regular files and directories can be synced: {path}")
                if self._fingerprint(before) != fingerprint:
                    raise SnapshotRace(f"File changed while preparing sync; retry: {path}")
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    self._check_cancelled()
                    digest.update(chunk)
                if self._fingerprint(os.fstat(stream.fileno())) != self._fingerprint(before):
                    raise SnapshotRace(f"File changed while preparing sync; retry: {path}")
                self._same_path(path, before)
                fingerprint = self._fingerprint(before)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                raise SnapshotRace(f"File moved while preparing sync; retry: {path}") from exc
            raise PodgroveError(f"Cannot read sync source {path}: {exc}") from exc
        return {"kind": "file", **fingerprint, "digest": digest.hexdigest()}

    def _snapshot(self, *, allow_missing: bool) -> dict[str, dict]:
        snapshot: dict[str, dict] = {}

        def visit(path: Path) -> None:
            self._check_cancelled()
            relative = path.relative_to(self.root).as_posix()
            if ".git" in path.relative_to(self.root).parts:
                return
            if excluded(relative, self.exclude):
                return
            try:
                info = path.lstat()
            except FileNotFoundError:
                if allow_missing:
                    return
                raise PodgroveError(f"Sync source does not exist: {path}") from None
            except OSError as exc:
                raise PodgroveError(f"Cannot inspect sync source {path}: {exc}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise PodgroveError(f"Symlinks are unsupported sync sources: {path}")
            if stat.S_ISDIR(info.st_mode):
                snapshot[relative] = {"kind": "directory", "mode": stat.S_IMODE(info.st_mode),
                                      "uid": info.st_uid, "gid": info.st_gid}
                try:
                    children = sorted(path.iterdir())
                except OSError as exc:
                    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                        raise SnapshotRace(f"Directory moved while preparing sync; retry: {path}") from exc
                    raise PodgroveError(f"Cannot list sync source {path}: {exc}") from exc
                for child in children:
                    visit(child)
                self._same_path(path, info)
            elif stat.S_ISREG(info.st_mode):
                snapshot[relative] = self._file_entry(path, info, self._baseline.get(relative))
            else:
                raise PodgroveError(f"Only regular files and directories can be synced: {path}")

        for path in self.paths:
            self._validate_source(path, require_exists=not allow_missing)
            # Parents must exist remotely even for a single-file bind. Record
            # their modes, but never recursively scan unrequested siblings.
            for parent in reversed((path.parent, *path.parent.parents)):
                if parent == self.root or self.root in parent.parents:
                    try:
                        info = parent.stat()
                    except FileNotFoundError:
                        if allow_missing:
                            continue
                        raise PodgroveError(f"Sync source does not exist: {parent}") from None
                    snapshot[parent.relative_to(self.root).as_posix()] = {
                        "kind": "directory", "mode": stat.S_IMODE(info.st_mode),
                        "uid": info.st_uid, "gid": info.st_gid
                    }
            visit(path)
        return snapshot

    def _same_path(self, path: Path, before: os.stat_result) -> None:
        try:
            after = path.lstat()
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise SnapshotRace(f"Source moved while preparing sync; retry: {path}") from exc
        if self._fingerprint(after) != self._fingerprint(before):
            raise SnapshotRace(f"Source changed while preparing sync; retry: {path}")

    def _retry_local(self, operation):
        for attempt in range(len(LOCAL_RETRY_DELAYS) + 1):
            self._check_cancelled()
            try:
                return operation()
            except SnapshotRace:
                if attempt == len(LOCAL_RETRY_DELAYS):
                    raise
                self._cancelled.wait(LOCAL_RETRY_DELAYS[attempt])

    def _docker(self, *args: str, stdin: BinaryIO | None = None, check: bool = True) -> subprocess.CompletedProcess:
        process = None
        try:
            with self._process_lock:
                self._check_cancelled()
                process = subprocess.Popen([self.docker, *args], env=self.env, stdin=stdin,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self._process = process
            stdout, stderr = process.communicate(timeout=300)
            result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            raise PodgroveError(f"Docker file sync failed: {exc}") from exc
        except BaseException:
            # The main-thread supervisor can be interrupted during initial sync.
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            raise
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None
        self._check_cancelled()
        if check and result.returncode:
            detail = result.stderr.decode(errors="replace").strip()
            raise PodgroveError(f"Docker file sync failed ({args[0]}): {detail or 'command failed'}")
        return result

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise PodgroveError("File synchronization cancelled")

    def cancel(self) -> None:
        """Interrupt the one owned Docker child before joining the sync worker."""
        self._cancelled.set()
        with self._process_lock:
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            if self._receiver is not None:
                self._receiver.cancel()

    def _start_receiver(self) -> None:
        if self._receiver is None:
            nonce = uuid.uuid4().hex
            command = [self.docker, "exec", "-i", self.container, "sh", "-c", RECEIVER,
                       "podgrove-sync-stream", nonce, _RECEIVE + _APPLY]
            with self._process_lock:
                self._check_cancelled()
                self._receiver = TarStream(command, self.env, self._cancelled, nonce)
            self._receiver.ready()

    def _send_archive(self, archive: BinaryIO) -> None:
        self._start_receiver()
        self._receiver.transfer(archive)

    def _inspect_labels(self, kind: str, name: str) -> dict | None:
        field = ".Config.Labels" if kind == "container" else ".Labels"
        result = self._docker(kind, "inspect", "--format", "{{json " + field + "}}", name, check=False)
        if result.returncode:
            error = result.stderr.decode(errors="replace")
            if "no such" in error.lower() or "not found" in error.lower():
                return None
            raise PodgroveError(f"Cannot inspect sync {kind}: {error.strip()}")
        try:
            labels = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PodgroveError(f"Invalid labels on sync {kind} {name}") from exc
        if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in self.labels.items()):
            raise PodgroveError(f"Refusing unowned sync {kind}: {name}")
        return labels

    def start(self) -> None:
        """Initialize the mirror completely before Compose creates any service."""
        if self._started:
            self.sync_once()
            return
        # A second preflight catches edits between construction and startup.
        self._retry_local(lambda: self._snapshot(allow_missing=False))
        if not self.paths:
            self._started = True
            return
        if self._inspect_labels("container", self.container) is not None:
            self._docker("rm", "--force", self.container)
        if self._inspect_labels("volume", self.volume) is None:
            args = ["volume", "create"]
            for key, value in self.labels.items():
                args.extend(("--label", f"{key}={value}"))
            self._docker(*args, self.volume)
        args = ["run", "--detach", "--name", self.container, "--network", "none",
                "--security-opt", "no-new-privileges:true"]
        for key, value in self.labels.items():
            args.extend(("--label", f"{key}={value}"))
        args.extend(("--volume", f"{self.root}:/workspace", "--mount",
                     f"type=volume,source={self.volume},target=/metadata", "--entrypoint", "sh", _IMAGE,
                     "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done"))
        try:
            self._docker(*args)
            self._created = True
            initialized = self._docker("exec", self.container, "sh", "-c", _INITIALIZE)
            mirror_empty = initialized.stdout.strip() == b"empty"
            result = self._docker("exec", self.container, "sh", "-c",
                                  "if [ -f /metadata/baseline.json ]; then cat /metadata/baseline.json; fi")
            if result.stdout.strip() and not mirror_empty:
                try:
                    saved = json.loads(result.stdout)
                    if saved.get("version") != 1 or saved.get("root") != str(self.root):
                        raise ValueError("incompatible baseline")
                    entries = saved["entries"]
                    if not isinstance(entries, dict):
                        raise ValueError("invalid entries")
                    self._baseline = {path: entry for path, entry in entries.items() if self._in_scope(path)}
                    for path, entry in self._baseline.items():
                        if not isinstance(entry, dict) or entry.get("kind") not in ("file", "directory"):
                            raise ValueError(f"invalid baseline entry: {path}")
                except (ValueError, KeyError, AttributeError, TypeError) as exc:
                    raise PodgroveError("Invalid remote sync baseline; recreate this environment") from exc
            self._started = True
            # A reconnect may have no changed files. Still pay API setup during
            # startup, so its first later edit uses the already verified stream.
            self._start_receiver()
            self.sync_once()
        except BaseException:
            try:
                self.close()
            except Exception as cleanup_error:
                print(f"Sync cleanup after startup failure: {cleanup_error}", file=sys.stderr, flush=True)
            raise

    def _in_scope(self, relative: str) -> bool:
        if not isinstance(relative, str) or not relative or "\x00" in relative:
            return False
        parts = Path(relative).parts
        if Path(relative).is_absolute() or ".." in parts or ".git" in parts:
            return False
        if excluded(relative, self.exclude):
            return False
        path = self.root / relative
        return any(path == source or source in path.parents or path in source.parents for source in self.paths)

    @staticmethod
    def _content_equal(before: dict, after: dict) -> bool:
        return all(before.get(key) == after.get(key) for key in ("kind", "mode", "digest", "uid", "gid"))

    def sync_once(self) -> int:
        """Apply local edits, additions and tracked deletions; return their count."""
        return self._retry_local(self._sync_once)

    def _sync_once(self) -> int:
        if not self._started:
            raise PodgroveError("File synchronizer has not been started")
        if self._receiver is not None:
            self._receiver.check()
        if not self.paths:
            return 0
        started = time.monotonic()
        current = self._snapshot(allow_missing=True)
        snapshot_finished = time.monotonic()
        changed = {path for path, entry in current.items()
                   if path not in self._baseline or not self._content_equal(self._baseline[path], entry)}
        deleted = {path for path, entry in self._baseline.items()
                   if path not in current or current[path]["kind"] != entry["kind"]}
        deleted.discard(".")
        if not changed and not deleted:
            self._baseline = current
            return 0
        if self.activity_callback is not None:
            self.activity_callback()
        directories = sorted((p for p, e in current.items() if e["kind"] == "directory"),
                             key=lambda p: (p.count("/"), p))
        files = sorted(p for p in changed if current[p]["kind"] == "file")
        directory_modes = sorted((p for p in changed if current[p]["kind"] == "directory"),
                                 key=lambda p: (-p.count("/"), p))
        removed = sorted(deleted, key=lambda p: (-p.count("/"), p))
        metadata = json.dumps({"version": 1, "root": str(self.root), "entries": current},
                              sort_keys=True, separators=(",", ":")).encode()
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for name, paths in (("deleted", removed), ("directories", directories),
                                    ("files", files), ("directory_modes", directory_modes)):
                    data = b"".join(os.fsencode(path) + b"\0" for path in paths)
                    self._add_bytes(tar, f"podgrove-transfer/control/{name}", data)
                self._add_bytes(tar, "podgrove-transfer/control/baseline.json", metadata)
                for relative in directory_modes:
                    self._check_cancelled()
                    info = tarfile.TarInfo(f"podgrove-transfer/payload/{relative}")
                    info.type = tarfile.DIRTYPE
                    info.mode = current[relative]["mode"]
                    info.uid = current[relative]["uid"]
                    info.gid = current[relative]["gid"]
                    tar.addfile(info)
                for relative in files:
                    self._check_cancelled()
                    self._add_file(tar, relative, current[relative])
            archive.seek(0)
            # Reuse the UID-guarded Docker exec stream for the whole session.
            # Only a matching post-commit acknowledgement advances our baseline.
            archive_finished = time.monotonic()
            self._send_archive(archive)
            transfer_finished = time.monotonic()
        self.last_timing = {"snapshot_seconds": snapshot_finished - started,
                            "archive_seconds": archive_finished - snapshot_finished,
                            "transport_seconds": transfer_finished - archive_finished}
        self._baseline = current
        return len(changed | deleted)

    @staticmethod
    def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
        import io
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(data))

    def _add_file(self, tar: tarfile.TarFile, relative: str, entry: dict) -> None:
        path = self.root / relative
        try:
            fd = self._open_file(path)
            with os.fdopen(fd, "rb") as stream:
                actual = os.fstat(stream.fileno())
                if not stat.S_ISREG(actual.st_mode) or any(
                    entry.get(key) != value for key, value in self._fingerprint(actual).items()
                ):
                    raise SnapshotRace(f"File changed while preparing sync; retry: {path}")
                info = tarfile.TarInfo(f"podgrove-transfer/payload/{relative}")
                info.size = actual.st_size
                info.mode = entry["mode"]
                info.uid = entry["uid"]
                info.gid = entry["gid"]
                info.mtime = actual.st_mtime
                try:
                    tar.addfile(info, stream)
                except OSError as exc:
                    if self._fingerprint(os.fstat(stream.fileno())) != self._fingerprint(actual):
                        raise SnapshotRace(f"File changed while preparing sync; retry: {path}") from exc
                    raise
                if self._fingerprint(os.fstat(stream.fileno())) != self._fingerprint(actual):
                    raise SnapshotRace(f"File changed while preparing sync; retry: {path}")
                self._same_path(path, actual)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                raise SnapshotRace(f"File moved while preparing sync; retry: {path}") from exc
            raise PodgroveError(f"Cannot archive sync source {path}: {exc}") from exc

    def close(self) -> None:
        """Remove this owned helper; the mirror and baseline survive reconnects."""
        if self._receiver is not None:
            self._receiver.close()
            self._receiver = None
        self._cancelled.clear()  # The owning worker has stopped before helper cleanup.
        if self._created:
            if self._inspect_labels("container", self.container) is not None:
                self._docker("rm", "--force", self.container)
            self._created = False
        self._started = False
