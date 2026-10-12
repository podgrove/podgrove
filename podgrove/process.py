"""Subprocess boundaries. Never invoke a shell or inherit a different Docker context."""
from __future__ import annotations

import codecs
import os
import selectors
import signal
import subprocess
import threading
import time
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
        timeout: float = 60, check: bool = True, cancel_event=None, on_output=None) -> subprocess.CompletedProcess:
    try:
        if on_output is not None:
            if input is not None:
                raise ValueError("Streaming commands cannot take buffered input")
            result = _streaming(args, env=env, cwd=cwd, timeout=timeout,
                                cancel_event=cancel_event, on_output=on_output)
        elif cancel_event is None:
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


def _streaming(args, *, env, cwd, timeout, cancel_event, on_output):
    if cancel_event is not None and cancel_event.is_set():
        raise PodgroveError(f"{args[0]} cancelled")
    process = subprocess.Popen(args, env=env, cwd=cwd, start_new_session=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    tails = {"stdout": "", "stderr": ""}
    pending = dict(tails)
    decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in tails}

    def receive(name, chunk, final=False):
        text = decoders[name].decode(chunk, final=final)
        tails[name] = (tails[name] + text)[-1024 * 1024:]
        text = pending[name] + text.replace("\r", "\n")
        lines = text.split("\n")
        pending[name] = lines.pop()
        for line in lines:
            if line:
                on_output(name, line)
        if pending[name] and (final or len(pending[name]) > 65536):
            on_output(name, pending[name])
            pending[name] = ""

    try:
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while selector.get_map() or process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                raise PodgroveError(f"{args[0]} cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            for key, _ in selector.select(min(.1, remaining)):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                receive(key.data, chunk, final=not chunk)
                if not chunk:
                    selector.unregister(key.fileobj)
        return subprocess.CompletedProcess(args, process.returncode, tails["stdout"], tails["stderr"])
    finally:
        selector.close()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def _cancellable(args, *, env, cwd, input, timeout, cancel_event):
    if cancel_event.is_set():
        raise PodgroveError(f"{args[0]} cancelled")
    process = subprocess.Popen(args, env=env, cwd=cwd, text=True, start_new_session=True,
                               stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finished = threading.Event()
    termination_lock = threading.Lock()
    terminated = False
    termination_error = None

    def terminate():
        nonlocal terminated, termination_error
        # An exited leader can leave pipe-holding helpers; signal its owned group exactly once.
        with termination_lock:
            if termination_error is not None:
                raise termination_error
            if terminated:
                return
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as error:
                termination_error = error
                raise
            terminated = True

    def cancel():
        while not finished.wait(.1):
            if cancel_event.is_set():
                try:
                    terminate()
                except OSError:
                    pass  # The synchronous cleanup path reports the retained first failure.
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
        cleanup_error = None
        try:
            terminate()
        except OSError as error:
            cleanup_error = error
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired) as error:
            if cleanup_error is None:
                cleanup_error = error
        if monitor.ident is not None:
            monitor.join(timeout=1)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError as error:
                    if cleanup_error is None:
                        cleanup_error = error
        if cleanup_error is not None:
            raise cleanup_error
