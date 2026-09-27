"""Private durable state and a per-environment process lock."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from .errors import PodgroveError
from .kube import resolve_namespace_mode


def identity(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]


def state_home(*, create: bool = True) -> Path:
    base = Path(os.environ.get("PODGROVE_STATE_HOME", str(Path.home() / ".local/state/podgrove")))
    if create:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        base.chmod(0o700)
    return base


def state_path(root: Path, context: str) -> Path:
    cluster = hashlib.sha256(context.encode()).hexdigest()[:8]
    return state_home() / f"{identity(root)}-{cluster}.json"


def read(path: Path) -> dict:
    try:
        _ordinary(path)
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PodgroveError("No usable environment state; run podgrove up first") from exc
    if not isinstance(data, dict):
        raise PodgroveError("Invalid environment state: expected an object")
    return data


def validate_binding(data: dict, root: Path, context: str) -> None:
    for key in ("identity", "root", "context", "namespace"):
        if not isinstance(data.get(key), str):
            raise PodgroveError(f"Invalid environment state: missing {key}")
    if data["identity"] != identity(root) or Path(data["root"]).resolve() != root.resolve() or data["context"] != context:
        raise PodgroveError("Environment state belongs to a different worktree or cluster; refusing the operation")
    namespace_mode(data)


def namespace_mode(data: dict) -> str:
    """Return saved mode, with legacy inference only when the field is absent."""
    if "namespace_mode" in data and data["namespace_mode"] is None:
        raise PodgroveError("Invalid environment state: namespace_mode cannot be null")
    return resolve_namespace_mode(data.get("namespace"), data.get("namespace_mode"))


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _ordinary(path: Path, *, socket_file=False):
    info = path.lstat()
    expected = stat.S_ISSOCK if socket_file else stat.S_ISREG
    if info.st_uid != os.getuid() or not expected(info.st_mode):
        raise PodgroveError(f"Refusing to use a foreign or unexpected local state file: {path.name}")
    return info


def _validate_path(path: Path, data: dict) -> None:
    root, context = data.get("root"), data.get("context")
    if not isinstance(root, str) or not Path(root).is_absolute() or not isinstance(context, str):
        raise PodgroveError("Invalid local environment binding")
    validate_binding(data, Path(root), context)
    cluster = hashlib.sha256(context.encode()).hexdigest()[:8]
    if (path.parent.resolve() != state_home(create=False).resolve()
            or path.name != f"{data['identity']}-{cluster}.json"):
        raise PodgroveError("Environment state filename does not match its worktree and cluster")


def local_records(context: str, namespace: str | None = None) -> list[dict]:
    """List verified records and scoped errors without creating local state."""
    if not context:
        raise PodgroveError("Set --context or PODGROVE_CONTEXT explicitly")
    home = state_home(create=False)
    cluster = hashlib.sha256(context.encode()).hexdigest()[:8]
    records = []
    for path in sorted(home.glob(f"*-{cluster}.json")):
        if not re.fullmatch(r"[a-f0-9]{12}-[a-f0-9]{8}\.json", path.name):
            continue
        try:
            _ordinary(path)
            data = read(path)
            _validate_path(path, data)
            if data["context"] == context and (namespace is None or data["namespace"] == namespace):
                records.append({"path": path, "data": data})
        except (OSError, PodgroveError) as exc:
            records.append({"path": path, "error": str(exc)})
    return records


def list_states(context: str) -> list[tuple[Path, dict]]:
    """Return local ownership evidence; never trust cluster lease paths."""
    return [(entry["path"], entry["data"]) for entry in local_records(context) if "data" in entry]


def cleanup(path: Path, data: dict) -> None:
    """Remove this stopped environment's files while the caller holds its lock.

    JSON is removed last so a failed local cleanup retains its retry binding.
    Lock removal belongs to lock() after the complete critical section.
    """
    try:
        _cleanup(path, data)
    except OSError as exc:
        raise PodgroveError(f"Local environment cleanup failed; retry down: {exc}") from exc


def _cleanup(path: Path, data: dict) -> None:
    _validate_path(path, data)
    files = [path.with_suffix(".log")]
    pattern = re.compile(re.escape(path.stem) + r"\.[0-9]+(?:\.[a-f0-9]{32})?\.tmp")
    files.extend(item for item in path.parent.glob(path.stem + ".*.tmp") if pattern.fullmatch(item.name))
    for item in files:
        try:
            _ordinary(item)
            item.unlink()
        except FileNotFoundError:
            pass
    if data.get("socket"):
        token = data.get("token", "")
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
            raise PodgroveError("Refusing cleanup of an unrecognized session socket path")
        expected = Path(tempfile.gettempdir()) / f"podgrove-{os.getuid()}-{token[:16]}.sock"
        if not isinstance(data["socket"], str) or Path(data["socket"]) != expected:
            raise PodgroveError("Refusing cleanup of an unrecognized session socket path")
        try:
            _ordinary(expected, socket_file=True)
            expected.unlink()
        except FileNotFoundError:
            pass
    try:
        _ordinary(path)
        path.unlink()
    except FileNotFoundError:
        pass


@contextlib.contextmanager
def lock(path: Path):
    lock_path = path.with_suffix(".lock")
    while True:
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            continue
        except OSError as exc:
            raise PodgroveError("Cannot safely open the environment lock") from exc
        stream = os.fdopen(fd, "a")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise PodgroveError("Another podgrove command is operating on this environment") from exc
        held = os.fstat(stream.fileno())
        try:
            current = _ordinary(lock_path)
            matches = (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)
        except FileNotFoundError:
            matches = False
        except BaseException:
            stream.close()
            raise
        if matches:
            break
        stream.close()  # The pathname was replaced while this descriptor waited.
    try:
        yield
    finally:
        # Critical work is finished. A new pathname may be created after unlink;
        # a waiter that opened this old inode must fail its post-flock identity check.
        try:
            try:
                current = lock_path.lstat()
                if (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino):
                    lock_path.unlink()
            except FileNotFoundError:
                pass
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
            stream.close()
        if path.parent.resolve() == state_home(create=False).resolve():
            try:
                path.parent.rmdir()
            except OSError:
                pass  # Other environments or retry evidence remain.
