"""Advisory repository labels from bounded local metadata, without Git commands.

Only .git, commondir and HEAD are read. Remote URLs, credentials, Git config,
hooks, objects and indexes are never opened, and no metadata is modified.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from collections.abc import Mapping

_MAX_METADATA = 4096


def _read(path: Path) -> str | None:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_METADATA:
                return None
            data = os.read(descriptor, _MAX_METADATA + 1)
            if len(data) > _MAX_METADATA:
                return None
            value = data.decode("utf-8").strip()
            return value if not any(ord(char) < 32 or ord(char) == 127 for char in value) else None
        finally:
            os.close(descriptor)
    except (OSError, UnicodeError):
        return None


def _directory(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_dir()
    except OSError:
        return False


def _metadata(root: Path) -> tuple[Path, Path] | None:
    dotgit = root / ".git"
    if _directory(dotgit):
        gitdir = dotgit
    else:
        pointer = _read(dotgit)
        if not pointer or not pointer.startswith("gitdir: "):
            return None
        target = pointer[len("gitdir: "):]
        if not target:
            return None
        gitdir = root / target
        if not _directory(gitdir):
            return None
    common = _read(gitdir / "commondir")
    common_dir = gitdir / common if common else gitdir
    if not _directory(common_dir):
        return None
    # Normalize '..' from linked-worktree commondir without following symlinks.
    return gitdir, Path(os.path.abspath(common_dir))


def _branch(head: str | None) -> str:
    if head and re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", head):
        return "detached-" + head[:12].lower()
    prefix = "ref: refs/heads/"
    if head and head.startswith(prefix):
        branch = head[len(prefix):]
        if (branch and not re.search(r"[\s~^:?*\[\\]", branch) and ".." not in branch and "@{" not in branch
                and not branch.endswith(".") and not any(not part or part.startswith(".") or part.endswith(".lock")
                                                          for part in branch.split("/"))):
            return branch
    return "unspecified"


def repository_labels(root: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return label sources; the manifest renderer applies Kubernetes syntax.

    Labels describe creation-time checkout metadata, not current remote state.
    Missing, unsupported or unreadable metadata uses the directory/unspecified
    fallbacks. Explicit environment overrides retain their prior behavior.
    """
    environ = os.environ if environ is None else environ
    result = {"repo": root.name, "branch": "unspecified"}
    if "PODGROVE_REPO" not in environ or "PODGROVE_BRANCH" not in environ:
        paths = _metadata(root)
        if paths:
            gitdir, common = paths
            if common.name == ".git":
                result["repo"] = common.parent.name
            elif common.name.endswith(".git") and common.name != ".git":
                result["repo"] = common.name[:-4]
            result["branch"] = _branch(_read(gitdir / "HEAD"))
    for field, variable in (("repo", "PODGROVE_REPO"), ("branch", "PODGROVE_BRANCH")):
        if variable in environ:
            result[field] = environ[variable]
    return result
