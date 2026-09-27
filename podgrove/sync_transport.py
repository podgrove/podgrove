"""Bounded, acknowledged tar batches over one owned Docker exec stream."""
from __future__ import annotations

import os
import math
import re
import selectors
import subprocess
import threading
import time
from typing import BinaryIO

from .errors import PodgroveError

CHUNK_BYTES = 65536
STDERR_BYTES = 4096
HEADER_BYTES = 72
MAX_FRAME_BYTES = 10**16 - 1

# The archive is fully received before the existing tar/apply program runs in
# its own shell. Its EXIT traps cannot replace this loop's frame cleanup trap.
# User file paths occur only inside the tar, never in this shell program.
RECEIVER = r'''
set -eu
nonce=$1
apply=$2
frame=''
trap 'if [ -n "$frame" ]; then rm -rf "$frame"; fi' EXIT
counter=1
printf 'READY %s\n' "$nonce"
while :; do
    frame=$(mktemp -d /tmp/podgrove-frame.XXXXXX)
    dd bs=65536 count=72 iflag=count_bytes,fullblock status=none > "$frame/header"
    header_bytes=$(stat -c %s "$frame/header")
    [ "$header_bytes" != 0 ] || exit 0
    [ "$header_bytes" = 72 ] || { echo 'Truncated sync frame header' >&2; exit 1; }
    IFS=' ' read -r magic frame_nonce sequence length extra < "$frame/header"
    [ "$magic" = PGS1 ] && [ "$frame_nonce" = "$nonce" ] && [ -z "$extra" ] || exit 1
    [ "$sequence" = "$(printf '%016d' "$counter")" ] || exit 1
    [ "${#length}" = 16 ] || exit 1
    case "$length" in *[!0-9]*) exit 1 ;; esac
    count=$(printf '%s' "$length" | sed 's/^0*//')
    [ -n "$count" ] || { echo 'Empty sync frame' >&2; exit 1; }
    dd bs=65536 count="$count" iflag=count_bytes,fullblock status=none > "$frame/archive.tar"
    received=$(stat -c %s "$frame/archive.tar")
    [ "$(printf '%016d' "$received")" = "$length" ] || { echo 'Truncated sync frame body' >&2; exit 1; }
    sh -c "$apply" < "$frame/archive.tar"
    rm -rf "$frame"
    frame=''
    printf 'ACK %s %016d\n' "$nonce" "$counter"
    counter=$((counter + 1))
done
'''


class TarStream:
    """One outstanding frame; an uncertain acknowledgement poisons the stream."""

    def __init__(self, command: list[str], env: dict, cancelled: threading.Event,
                 nonce: str, timeout: float = 300):
        if not re.fullmatch(r"[a-f0-9]{32}", nonce) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid sync stream nonce or timeout")
        self.nonce, self.cancelled, self.timeout = nonce, cancelled, timeout
        self.sequence = 1
        self._failure = None
        self._ready = False
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        try:
            self.process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            raise PodgroveError(f"Cannot start file sync stream: {exc}") from exc
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        self._reader = threading.Thread(target=self._read_stderr, name="podgrove-sync-stderr", daemon=True)
        self._reader.start()

    def _read_stderr(self):
        try:
            while chunk := self.process.stderr.read(CHUNK_BYTES):
                with self._stderr_lock:
                    self._stderr.extend(chunk)
                    del self._stderr[:-STDERR_BYTES]
        except OSError:
            pass

    def _error(self, message):
        with self._stderr_lock:
            detail = bytes(self._stderr).decode(errors="replace").strip()
        return PodgroveError(f"File sync stream {message}" + (f": {detail}" if detail else ""))

    def _check(self):
        if self.cancelled.is_set():
            raise PodgroveError("File synchronization cancelled")
        if self._failure:
            raise self._error(self._failure)
        if self.process.poll() is not None:
            self._reader.join(timeout=1)
            raise self._error("exited; run podgrove up to reconnect")

    def check(self):
        self._check()
        if self._ready:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if selector.select(timeout=0):
                    self._failure = "closed or sent an unexpected idle acknowledgement; reconnect required"
                    self.cancel()
                    raise self._error(self._failure)

    def ready(self):
        self._exchange(b"", None, 0, f"READY {self.nonce}\n".encode())
        self._ready = True

    def transfer(self, archive: BinaryIO):
        self.check()
        if not self._ready:
            raise self._error("has not completed its ready handshake")
        archive.seek(0, os.SEEK_END)
        size = archive.tell()
        archive.seek(0)
        if not 0 < size <= MAX_FRAME_BYTES or self.sequence > MAX_FRAME_BYTES:
            raise self._error("archive size or sequence exceeds protocol bounds")
        header = f"PGS1 {self.nonce} {self.sequence:016d} {size:016d}\n".encode()
        assert len(header) == HEADER_BYTES
        self._exchange(header, archive, size, f"ACK {self.nonce} {self.sequence:016d}\n".encode())
        self.sequence += 1

    def _exchange(self, header: bytes, archive: BinaryIO | None, remaining: int, expected: bytes):
        deadline = time.monotonic() + self.timeout
        pending, response = memoryview(header), bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ, "output")
                if pending or remaining:
                    selector.register(self.process.stdin, selectors.EVENT_WRITE, "input")
                while True:
                    self._check()
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise self._error("timed out awaiting a committed batch; reconnect required")
                    for key, _events in selector.select(timeout=min(0.2, left)):
                        if key.data == "input":
                            if not pending:
                                chunk = archive.read(min(CHUNK_BYTES, remaining))
                                if not chunk:
                                    raise self._error("local archive ended before its declared length")
                                remaining -= len(chunk)
                                pending = memoryview(chunk)
                            written = os.write(self.process.stdin.fileno(), pending)
                            pending = pending[written:]
                            if not pending and not remaining:
                                selector.unregister(self.process.stdin)
                        else:
                            chunk = os.read(self.process.stdout.fileno(), 4096)
                            if not chunk:
                                self._reader.join(timeout=0.1)
                                raise self._error("closed before acknowledging its batch; reconnect required")
                            response.extend(chunk)
                            if len(response) > len(expected) or not expected.startswith(response):
                                raise self._error("returned an invalid acknowledgement; reconnect required")
                            if response == expected:
                                if pending or remaining:
                                    raise self._error("acknowledged a batch before receiving its complete archive")
                                self._check()
                                return
        except BaseException as exc:
            self._failure = str(exc)
            self.cancel()
            if isinstance(exc, OSError):
                raise self._error("transport failed; reconnect required") from exc
            raise

    def cancel(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)

    def close(self):
        self.cancel()
        self._reader.join(timeout=2)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()
