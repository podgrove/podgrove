"""Subprocess boundaries. Never invoke a shell or inherit a different Docker context."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .errors import PodgroveError


def docker_environment(host: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_HOST", "DOCKER_TLS", "BUILDX_BUILDER"):
        env.pop(key, None)
    env["DOCKER_HOST"] = host
    env["COMPOSE_ANSI"] = "never"
    return env


def run(args: list[str], *, env=None, cwd: Path | None = None, input: str | None = None,
        timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, env=env, cwd=cwd, input=input, text=True,
                                capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise PodgroveError(f"Required executable not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PodgroveError(f"{args[0]} timed out after {timeout:g}s") from exc
    if check and result.returncode:
        # No command argv: compose exec/build arguments can contain dev credentials.
        raise PodgroveError(f"{args[0]} failed ({result.returncode}): {result.stderr.strip()[-3000:]}")
    return result
