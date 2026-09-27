"""Subprocess boundaries. Never invoke a shell or inherit a different Docker context."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
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
        timeout: float = 60, check: bool = True, cancel_event=None) -> subprocess.CompletedProcess:
    try:
        if cancel_event is None:
            result = subprocess.run(args, env=env, cwd=cwd, input=input, text=True,
                                    capture_output=True, timeout=timeout, check=False)
        else:
            result = _cancellable(args, env=env, cwd=cwd, input=input, timeout=timeout,
                                  cancel_event=cancel_event)
    except FileNotFoundError as exc:
        raise PodgroveError(f"Required executable not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PodgroveError(f"{args[0]} timed out after {timeout:g}s") from exc
    if check and result.returncode:
        # No command argv: compose exec/build arguments can contain dev credentials.
        raise PodgroveError(f"{args[0]} failed ({result.returncode}): {result.stderr.strip()[-3000:]}")
    return result


def _cancellable(args, *, env, cwd, input, timeout, cancel_event):
    if cancel_event.is_set():
        raise PodgroveError(f"{args[0]} cancelled")
    process = subprocess.Popen(args, env=env, cwd=cwd, text=True, start_new_session=True,
                               stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finished = threading.Event()

    def terminate():
        # The leader can exit while an authentication helper still holds our
        # capture pipes. Its private process group remains ours to terminate.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def cancel():
        while not finished.wait(.1):
            if cancel_event.is_set():
                terminate()
                return

    monitor = threading.Thread(target=cancel, name="podgrove-command-cancel", daemon=True)
    try:
        monitor.start()
        stdout, stderr = process.communicate(input=input, timeout=timeout)
        if cancel_event.is_set():
            raise PodgroveError(f"{args[0]} cancelled")
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    finally:
        finished.set()
        terminate()
        process.wait(timeout=2)
        if monitor.ident is not None:
            monitor.join(timeout=1)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
