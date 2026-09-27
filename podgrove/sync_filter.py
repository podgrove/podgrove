"""Explicit workspace-relative excludes for mirrored files (never Compose builds)."""
from __future__ import annotations

from fnmatch import fnmatchcase
from functools import lru_cache

from .errors import PodgroveError


def validate_patterns(patterns: list[str]) -> list[str]:
    for pattern in patterns:
        if (not pattern or pattern.startswith(("/", "!")) or "\\" in pattern
                or any(ord(char) < 32 or ord(char) == 127 for char in pattern)
                or any(part in ("", ".", "..") for part in pattern.rstrip("/").split("/"))):
            raise PodgroveError("sync.exclude: use relative POSIX globs without negation, empty components, or '..'")
    return patterns


@lru_cache(maxsize=4096)
def _match(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return _match(parts, pattern[1:]) or bool(parts and _match(parts[1:], pattern))
    return bool(parts and fnmatchcase(parts[0], pattern[0]) and _match(parts[1:], pattern[1:]))


def excluded(relative: str, patterns: list[str]) -> bool:
    """A matched ancestor excludes its subtree; slashless globs match any component."""
    parts = tuple(relative.split("/"))
    if relative in ("", "."):
        return False
    for raw in patterns:
        pattern = raw.rstrip("/")
        if "/" not in pattern:
            if any(fnmatchcase(part, pattern) for part in parts):
                return True
        elif any(_match(parts[:length], tuple(pattern.split("/"))) for length in range(1, len(parts) + 1)):
            return True
    return False
