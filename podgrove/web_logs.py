"""Bounded, cancellable read-only log followers for the local dashboard.

Each connection follows fixed resource/container identities for at most five
minutes. Reopening starts a fresh tail; it is not an exactly-once resume cursor.
Only complete lines are exposed, after common credential-pattern redaction.
"""
from __future__ import annotations

import http.client
import os
import queue
import re
import selectors
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import urlencode

MAX_LINE_BYTES = 16 * 1024
MAX_FRAME_BYTES = 1024 * 1024
MAX_REPLICAS = 8
QUEUE_LINES = 128
MAX_SECONDS = 300.0
VERIFY_SECONDS = 5.0
HEARTBEAT_SECONDS = 2.0
HEADER_SECONDS = 5.0
KEY_MARKER = re.compile(rb"-----(BEGIN|END) (?:[A-Z ]{1,32} )?PRIVATE KEY-----")


class FollowError(Exception):
    pass


class LineBuffer:
    """Retain bytes until newline; never disclose a clipped credential prefix."""
    def __init__(self, emit, redact, container=None):
        self.emit, self.redact, self.container = emit, redact, container
        self.pending = {"stdout": bytearray(), "stderr": bytearray()}
        self.dropping = {"stdout": False, "stderr": False}
        self.private_key = {"stdout": False, "stderr": False}
        self.sensitive_line = {"stdout": False, "stderr": False}
        self.marker_tail = {"stdout": b"", "stderr": b""}
        self.partial_lines_discarded = 0

    def _markers(self, stream, piece):
        # Track delimiters even when the line itself has already exceeded the
        # output limit. Keep only a bounded suffix for split marker detection.
        self.sensitive_line[stream] |= self.private_key[stream]
        previous = self.marker_tail[stream]
        combined = previous + piece
        for match in KEY_MARKER.finditer(combined):
            if match.end() <= len(previous):
                continue
            self.sensitive_line[stream] = True
            self.private_key[stream] = match[1] == b"BEGIN"
        self.marker_tail[stream] = combined[-128:]

    def feed(self, stream, chunk):
        while chunk:
            newline = chunk.find(b"\n")
            piece = chunk if newline < 0 else chunk[:newline + 1]
            chunk = b"" if newline < 0 else chunk[newline + 1:]
            self._markers(stream, piece)
            if not self.dropping[stream]:
                if len(self.pending[stream]) + len(piece) > MAX_LINE_BYTES:
                    self.pending[stream].clear()
                    self.dropping[stream] = True
                else:
                    self.pending[stream].extend(piece)
            if newline < 0:
                continue
            if self.dropping[stream]:
                self.emit({"type": "notice", "reason": "line_limit", "container": self.container,
                           "text": "An oversized log line was discarded."})
                self.dropping[stream] = False
                self.marker_tail[stream] = b""
                self.sensitive_line[stream] = self.private_key[stream]
                continue
            text = bytes(self.pending[stream]).decode("utf-8", errors="replace")
            self.pending[stream].clear()
            hidden = self.sensitive_line[stream]
            self.marker_tail[stream] = b""
            self.sensitive_line[stream] = self.private_key[stream]
            self.emit({"type": "line", "container": self.container, "stream": stream,
                       "text": "[REDACTED PRIVATE KEY]\n" if hidden else self.redact(text)})

    def finish(self):
        self.partial_lines_discarded += sum(bool(value) or self.dropping[key]
                                            for key, value in self.pending.items())
        for value in self.pending.values():
            value.clear()


class DockerFrames:
    """Incremental Docker multiplexed frames, or plain TTY bytes.

The legacy log endpoint can omit a useful content type. Its binary stream
header is distinguished before emitting bytes; later malformed framing fails.
"""
    def __init__(self, consume):
        self.consume = consume
        self.prefix = bytearray()
        self.mode = None
        self.remaining = 0
        self.stream = "stdout"

    def feed(self, chunk):
        while chunk:
            if self.mode == "plain":
                self.consume("stdout", chunk)
                return
            if self.remaining:
                size = min(self.remaining, len(chunk))
                self.consume(self.stream, chunk[:size])
                self.remaining -= size
                chunk = chunk[size:]
                continue
            needed = 8 - len(self.prefix)
            self.prefix.extend(chunk[:needed])
            chunk = chunk[needed:]
            if self.mode is None and self.prefix[0] not in (0, 1, 2, 3):
                self.mode = "plain"
                self.consume("stdout", bytes(self.prefix))
                self.prefix.clear()
                continue
            if len(self.prefix) < 8:
                return
            framed = self.prefix[0] in (0, 1, 2, 3) and self.prefix[1:4] == b"\0\0\0"
            if self.mode is None and not framed:
                if self.prefix[0] in (0, 1, 2, 3):
                    raise FollowError("Invalid Docker stream header")
                self.mode = "plain"
                self.consume("stdout", bytes(self.prefix))
                self.prefix.clear()
                continue
            if not framed or self.prefix[0] not in (1, 2):
                raise FollowError("Invalid Docker stream framing")
            self.mode = "framed"
            self.stream = "stdout" if self.prefix[0] == 1 else "stderr"
            self.remaining = int.from_bytes(self.prefix[4:8], "big")
            self.prefix.clear()
            if self.remaining > MAX_FRAME_BYTES:
                raise FollowError("Docker frame exceeds the stream limit")

    def finish(self):
        if self.mode is None and self.prefix and self.prefix[0] not in (0, 1, 2, 3):
            self.consume("stdout", bytes(self.prefix))
            self.prefix.clear()
        if self.prefix or self.remaining:
            raise FollowError("Docker stream ended in an incomplete frame")


class LogStream:
    def __init__(self, backend, ident, *, source, service, tail, container=None):
        from .web import NAME, WebError
        if source not in ("engine", "service") or type(tail) is not int or not 1 <= tail <= 200:
            raise WebError("Select engine/service logs and a tail between 1 and 200", 400)
        if source == "service" and (not isinstance(service, str) or not NAME.fullmatch(service)):
            raise WebError("Select an existing Compose service", 400)
        if source == "engine" and (service is not None or container is not None):
            raise WebError("Engine logs do not accept service or container parameters", 400)
        if container is not None and (not isinstance(container, str) or not re.fullmatch(r"[a-f0-9]{64}", container)):
            raise WebError("Select a valid service container", 400)
        self.backend, self.ident, self.source, self.service = backend, ident, source, service
        self.tail, self.container = tail, container
        self.stopped = threading.Event()
        self.finished = threading.Event()
        self.records = queue.Queue(maxsize=QUEUE_LINES)
        self.max_seconds = MAX_SECONDS
        self.verify_seconds = VERIFY_SECONDS
        self.heartbeat_seconds = HEARTBEAT_SECONDS
        self.threads = []
        self.ready = []
        self.closers = []
        self.buffers = []
        self.lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.reason = None
        self.completed = 0
        self.started = None

    @staticmethod
    def _identity(engine):
        return tuple(item["metadata"]["uid"] for item in engine[1:])

    def _verify(self):
        # Re-read the local record as well: a reconnect can select another local
        # tunnel even while the resource names still look the same.
        current = self.backend._record(self.ident)
        if any(current.get(key) != self.data.get(key) for key in
               ("root", "namespace", "context", "token", "docker_host", "socket")):
            raise FollowError("Local environment binding changed")
        engine = self.backend._engine(self.data, cancel=self.stopped)
        if self._identity(engine) != self.uids:
            raise FollowError("Engine identity changed")

    def _emit(self, record):
        while not self.stopped.is_set():
            try:
                self.records.put(record, timeout=0.1)
                return
            except queue.Full:
                continue  # Bounded backpressure reaches the child/socket.

    def _register(self, close):
        with self.lock:
            self.closers.append(close)
            stopped = self.stopped.is_set()
        if stopped:
            close()

    def _fail(self, reason="source_unavailable"):
        with self.lock:
            if self.reason is None:
                self.reason = reason
            self.stopped.set()

    def start(self):
        from .web import WebError
        try:
            if self.stopped.is_set():
                raise WebError("Log stream was cancelled")
            self.data = self.backend._record(self.ident)
            self.engine = self.backend._engine(self.data, cancel=self.stopped)
            self.uids = self._identity(self.engine)
            if self.source == "service":
                rows = [row for row in self.backend._containers(self.data)
                        if row["Labels"]["com.docker.compose.service"] == self.service]
                if len({row["Labels"]["com.docker.compose.project"] for row in rows}) > 1:
                    raise WebError("Service name is ambiguous across Compose projects", 409)
                if self.container is not None:
                    rows = [row for row in rows if row["Id"] == self.container]
                if not rows:
                    raise WebError("Unknown Compose service or container", 404)
                if len(rows) > MAX_REPLICAS:
                    raise WebError("Select one container when a service has more than eight replicas", 409)
                self.containers = [row["Id"] for row in rows]
            else:
                self.containers = []
            if self.stopped.is_set():
                raise WebError("Log stream was cancelled")
            targets = self.containers or [None]
            for container in targets:
                ready = threading.Event()
                self.ready.append(ready)
                thread = threading.Thread(target=self._worker, args=(container, ready),
                                          name="podgrove-web-log-source", daemon=True)
                self.threads.append(thread)
            for thread in self.threads:
                thread.start()
            deadline = time.monotonic() + 8
            for ready in self.ready:
                while not ready.wait(0.05):
                    if self.stopped.is_set() or time.monotonic() >= deadline:
                        raise WebError("Log source could not be opened")
            if self.stopped.is_set():
                raise WebError("Log source is unavailable")
            self._verify()  # No buffered log byte leaves before post-open verification.
            self.started = time.monotonic()
            return {"type": "start", "source": self.source, "service": self.service,
                    "containers": self.containers, "tail": self.tail, "max_seconds": self.max_seconds,
                    "resume": "restart_with_tail", "gap_possible": True}
        except WebError:
            self.close("source_unavailable")
            raise
        except Exception as error:
            self.close("source_unavailable")
            raise WebError("Log source or engine ownership could not be verified") from error

    def _worker(self, container, ready):
        from .web import _redact_logs
        lines = LineBuffer(self._emit, _redact_logs, container)
        with self.lock:
            self.buffers.append(lines)
        try:
            if self.stopped.is_set():
                return
            if container is None:
                self._engine_follow(lines, ready)
            else:
                self._docker_follow(container, lines, ready)
                if not self.stopped.is_set():
                    self._emit({"type": "notice", "reason": "container_closed", "container": container,
                                "text": "A container log source ended; replacement containers are not followed automatically."})
        except Exception:
            if not self.stopped.is_set():
                self._fail()
        finally:
            lines.finish()
            ready.set()
            with self.lock:
                self.completed += 1
                if self.completed == len(self.threads):
                    self.finished.set()

    def _engine_follow(self, lines, ready):
        if self.stopped.is_set():
            return
        kube, _, pod, _ = self.engine
        command = kube.command("logs", "pod/" + pod["metadata"]["name"], "--container", "docker",
                               "--follow", "--tail", str(self.tail), "--timestamps=true")
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True, bufsize=0)

        def terminate():
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self._register(terminate)
        ready.set()
        stderr_bytes = 0
        verified = False
        try:
            with selectors.DefaultSelector() as poll:
                poll.register(process.stdout, selectors.EVENT_READ, "stdout")
                poll.register(process.stderr, selectors.EVENT_READ, "stderr")
                while poll.get_map() and not self.stopped.is_set():
                    for key, _ in poll.select(0.1):
                        chunk = os.read(key.fd, 16384)
                        if not chunk:
                            poll.unregister(key.fileobj)
                        elif key.data == "stdout":
                            if not verified:
                                try:
                                    self._verify()
                                except Exception:
                                    self._fail("ownership_changed")
                                    return
                                verified = True
                            lines.feed("stdout", chunk)
                        else:
                            stderr_bytes += len(chunk)
                            if stderr_bytes > 16384:
                                raise FollowError("Log transport stderr limit exceeded")
            if not self.stopped.is_set() and process.wait(timeout=1) != 0:
                raise FollowError("Log transport exited unsuccessfully")
        finally:
            terminate()
            process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()

    def _docker_follow(self, container, lines, ready):
        connection = self.backend._docker_connection(self.data, timeout=3)
        transport = connection.sock

        def disconnect():
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        self._register(disconnect)
        expired = threading.Event()
        header_deadline = time.monotonic() + HEADER_SECONDS

        def expire_headers():
            expired.set()
            disconnect()

        timer = threading.Timer(HEADER_SECONDS, expire_headers)
        timer.start()
        response = None
        decoder = DockerFrames(lines.feed)
        try:
            query = urlencode({"stdout": 1, "stderr": 1, "timestamps": 1, "tail": self.tail, "follow": 1})
            connection.request("GET", f"/containers/{container}/logs?{query}", headers={"Connection": "close"})
            response = connection.getresponse()
            if response.status != 200:
                raise FollowError("Docker refused the log source")
            timer.cancel()
            timer.join(timeout=1)
            if expired.is_set() or time.monotonic() >= header_deadline:
                raise FollowError("Docker log headers exceeded their deadline")
            transport.settimeout(None)  # Quiet logs stay attached; cancellation shuts this socket down.
            ready.set()
            while not self.stopped.is_set():
                chunk = response.read1(16384)
                if not chunk:
                    decoder.finish()
                    break
                decoder.feed(chunk)
        except (OSError, http.client.HTTPException) as error:
            raise FollowError("Docker log transport failed") from error
        finally:
            timer.cancel()
            timer.join(timeout=1)
            disconnect()
            if response is not None:
                response.close()
            connection.close()

    def events(self):
        verified = heartbeat = time.monotonic()
        try:
            while not self.stopped.is_set():
                now = time.monotonic()
                if now - self.started >= self.max_seconds:
                    self._fail("lifetime_limit")
                    break
                if now - verified >= self.verify_seconds:
                    try:
                        self._verify()
                    except Exception:
                        self._fail("ownership_changed")
                        break
                    verified = time.monotonic()
                try:
                    record = self.records.get(timeout=0.1)
                except queue.Empty:
                    if self.finished.is_set():
                        try:
                            self._verify()
                        except Exception:
                            self._fail("ownership_changed")
                        break
                else:
                    yield record
                if time.monotonic() - heartbeat >= self.heartbeat_seconds:
                    heartbeat = time.monotonic()
                    yield {"type": "heartbeat"}
        finally:
            self.close(self.reason or "completed")
        yield {"type": "end", "reason": self.reason or "completed", "gap_possible": True,
               "partial_lines_discarded": sum(buffer.partial_lines_discarded for buffer in self.buffers)}

    def close(self, reason="client_cancelled"):
        with self.close_lock:
            self._fail(reason)
            with self.lock:
                closers = list(self.closers)
            for close in closers:
                try:
                    close()
                except OSError:
                    pass
            deadline = time.monotonic() + 5
            for thread in self.threads:
                if thread.ident is not None and thread is not threading.current_thread():
                    thread.join(timeout=max(0, deadline - time.monotonic()))
