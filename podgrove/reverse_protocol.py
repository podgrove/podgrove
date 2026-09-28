"""Bounded bidirectional TCP multiplexing over an owned binary stdio channel."""
from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import os
import re
import selectors
import signal
import socket
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

HEADER = struct.Struct("!BII")
READY, OPEN, ACCEPT, DATA, CREDIT, END, CLOSE, PING, PONG = range(1, 10)
DATA_BYTES = 65536
WINDOW_BYTES = 256 * 1024
MAX_CONNECTIONS = 32
OUTPUT_BYTES = MAX_CONNECTIONS * (WINDOW_BYTES + DATA_BYTES) + DATA_BYTES
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_TIMEOUT = 30.0
CONNECT_TIMEOUT = 10.0


class ProtocolError(Exception):
    """The channel cannot preserve the existing TCP streams."""


@dataclass
class Connection:
    sock: socket.socket
    connecting: bool = False
    accepted: bool = False
    deadline: float = 0.0
    pending: bytearray = field(default_factory=bytearray)
    credit: int = WINDOW_BYTES
    available: int = WINDOW_BYTES
    read_closed: bool = False
    received_end: bool = False
    write_closed: bool = False
    sent: int = 0
    received: int = 0
    send_hash: object = field(default_factory=hashlib.sha256)
    receive_hash: object = field(default_factory=hashlib.sha256)


def reset(sock):
    """An uncertain transport ends with reset rather than successful EOF."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except OSError:
        pass
    sock.close()


class Peer:
    """Forward independent TCP half-streams without buffering whole requests."""

    def __init__(self, incoming, outgoing, *, nonce, ports, targets=None, listeners=None,
                 cancel=None, ready=None, allow_open=None, check_active=None, stderr=None,
                 max_connections=MAX_CONNECTIONS, heartbeat_interval=HEARTBEAT_INTERVAL,
                 heartbeat_timeout=HEARTBEAT_TIMEOUT):
        if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
            raise ValueError("Invalid reverse channel identity")
        if type(max_connections) is not int or not 1 <= max_connections <= MAX_CONNECTIONS:
            raise ValueError("Invalid reverse connection limit")
        if not 0 < heartbeat_interval < heartbeat_timeout:
            raise ValueError("Invalid reverse heartbeat budget")
        self.incoming, self.outgoing = incoming, outgoing
        self.stderr = stderr
        self.nonce, self.ports = nonce, list(ports)
        self.targets = targets
        self.listeners = listeners or {}
        self.remote = targets is None
        self.cancel = cancel or threading.Event()
        self.on_ready = ready or (lambda: None)
        self.allow_open = allow_open or (lambda: True)
        self.check_active = check_active or (lambda: True)
        self.max_connections = max_connections
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.connections = {}
        self.high_id = 0
        self.input_buffer = bytearray()
        self.output = deque()
        self.output_size = 0
        self.output_peak = 0
        self.connection_buffer_peak = 0
        self.failed_connections = 0
        self.bytes_sent = 0
        self.bytes_received = 0
        self.is_ready = False
        self.last_received = self.last_ping = time.monotonic()
        self.selector = None

    def snapshot(self):
        return {"active_connections": len(self.connections), "failed_connections": self.failed_connections,
                "sent_bytes": self.bytes_sent, "received_bytes": self.bytes_received,
                "output_buffer_peak": self.output_peak, "connection_buffer_peak": self.connection_buffer_peak}

    def _queue(self, kind, ident=0, payload=b""):
        if len(payload) > DATA_BYTES or self.output_size + HEADER.size + len(payload) > OUTPUT_BYTES:
            raise ProtocolError("Reverse channel buffer limit exceeded")
        data = HEADER.pack(kind, ident, len(payload)) + payload
        self.output.append(memoryview(data))
        self.output_size += len(data)
        self.output_peak = max(self.output_peak, self.output_size)

    def _monitor(self, target, events, label):
        try:
            self.selector.get_key(target)
        except KeyError:
            if events:
                self.selector.register(target, events, label)
        else:
            if events:
                self.selector.modify(target, events, label)
            else:
                self.selector.unregister(target)

    def _drop(self, ident, *, notify=True, failed=True):
        connection = self.connections.pop(ident, None)
        if connection is None:
            return
        self._monitor(connection.sock, 0, None)
        if failed:
            self.failed_connections += 1
            reset(connection.sock)
        else:
            connection.sock.close()
        if notify:
            self._queue(CLOSE, ident)

    def _finish(self, ident, connection):
        if connection.received_end and not connection.pending and not connection.write_closed:
            try:
                connection.sock.shutdown(socket.SHUT_WR)
            except OSError:
                self._drop(ident)
                return
            connection.write_closed = True
        if connection.read_closed and connection.write_closed:
            self._drop(ident, notify=False, failed=False)

    def _opened(self, ident, connection):
        connection.connecting = False
        connection.accepted = True
        self._queue(ACCEPT, ident)

    def _open(self, ident, payload):
        if self.remote or not self.is_ready or ident <= self.high_id or len(payload) != 2:
            raise ProtocolError("Invalid reverse connection request")
        self.high_id = ident
        port = struct.unpack("!H", payload)[0]
        if port not in self.targets:
            raise ProtocolError("Reverse connection requested an unconfigured port")
        if len(self.connections) >= self.max_connections or not self.allow_open():
            self.failed_connections += 1
            self._queue(CLOSE, ident)
            return
        host, port = self.targets[port]
        if host not in ("127.0.0.1", "::1") or type(port) is not int or not 1 <= port <= 65535:
            raise ProtocolError("Reverse target must be a configured literal loopback address")
        sock = socket.socket(socket.AF_INET6 if host == "::1" else socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        connection = Connection(sock, connecting=True, deadline=time.monotonic() + CONNECT_TIMEOUT)
        self.connections[ident] = connection
        try:
            result = sock.connect_ex((host, port))
        except OSError:
            self._drop(ident)
            return
        if result == 0:
            self._opened(ident, connection)
        elif result not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
            self._drop(ident)

    def _frame(self, kind, ident, payload):
        if kind in (PING, PONG):
            if ident or payload:
                raise ProtocolError("Invalid reverse heartbeat")
            if kind == PING:
                self._queue(PONG)
            return
        if kind == READY:
            expected = {"version": 1, "nonce": self.nonce, "ports": self.ports}
            try:
                valid = json.loads(payload) == expected
            except (ValueError, UnicodeError):
                valid = False
            if self.remote or self.is_ready or ident or not valid:
                raise ProtocolError("Invalid reverse listener readiness")
            self.is_ready = True
            self.on_ready()
            return
        if not self.is_ready or not ident:
            raise ProtocolError("Reverse data arrived before readiness")
        if kind == OPEN:
            self._open(ident, payload)
            return
        if kind not in (ACCEPT, DATA, CREDIT, END, CLOSE):
            raise ProtocolError("Unknown reverse frame")
        connection = self.connections.get(ident)
        if connection is None:
            if ident > self.high_id:
                raise ProtocolError("Unknown reverse connection")
            return
        if kind == CLOSE:
            if payload:
                raise ProtocolError("Invalid reverse close")
            self._drop(ident, notify=False)
        elif kind == ACCEPT:
            if not self.remote or connection.accepted or payload:
                raise ProtocolError("Invalid reverse connection acceptance")
            connection.accepted = True
        elif kind == CREDIT:
            if len(payload) != 4:
                raise ProtocolError("Invalid reverse flow control")
            amount = struct.unpack("!I", payload)[0]
            if not amount or connection.credit + amount > WINDOW_BYTES:
                raise ProtocolError("Reverse flow control exceeded its window")
            connection.credit += amount
        elif kind == DATA:
            if not connection.accepted or connection.received_end or not payload or len(payload) > connection.available:
                raise ProtocolError("Reverse stream exceeded its receive window")
            connection.available -= len(payload)
            connection.pending.extend(payload)
            connection.receive_hash.update(payload)
            connection.received += len(payload)
            self.bytes_received += len(payload)
            self.connection_buffer_peak = max(self.connection_buffer_peak, len(connection.pending))
        elif kind == END:
            proof = struct.pack("!Q", connection.received) + connection.receive_hash.digest()
            if connection.received_end or payload != proof:
                raise ProtocolError("Reverse stream completion could not be verified")
            connection.received_end = True
            self._finish(ident, connection)

    def _read_channel(self):
        try:
            data = os.read(self.incoming, min(DATA_BYTES, DATA_BYTES + HEADER.size - len(self.input_buffer)))
        except (BlockingIOError, InterruptedError):
            return
        if not data:
            raise ProtocolError("Reverse channel closed; active requests were not replayed")
        self.input_buffer.extend(data)
        while len(self.input_buffer) >= HEADER.size:
            kind, ident, size = HEADER.unpack_from(self.input_buffer)
            if size > DATA_BYTES:
                raise ProtocolError("Reverse frame exceeds the size limit")
            if len(self.input_buffer) < HEADER.size + size:
                break
            payload = bytes(self.input_buffer[HEADER.size:HEADER.size + size])
            del self.input_buffer[:HEADER.size + size]
            self._frame(kind, ident, payload)
            self.last_received = time.monotonic()

    def _flush_channel(self):
        try:
            count = os.write(self.outgoing, self.output[0][:DATA_BYTES])
        except (BlockingIOError, InterruptedError):
            return
        if count <= 0:
            raise ProtocolError("Reverse channel write failed")
        self.output_size -= count
        if count == len(self.output[0]):
            self.output.popleft()
        else:
            self.output[0] = self.output[0][count:]

    def _accept(self, port, listener):
        try:
            sock, _ = listener.accept()
        except (BlockingIOError, InterruptedError):
            return
        if len(self.connections) >= self.max_connections or self.high_id == 2**32 - 1:
            self.failed_connections += 1
            reset(sock)
            return
        sock.setblocking(False)
        self.high_id += 1
        self.connections[self.high_id] = Connection(sock)
        self._queue(OPEN, self.high_id, struct.pack("!H", port))

    def _socket(self, ident, events):
        connection = self.connections.get(ident)
        if connection is None:
            return
        try:
            if connection.connecting:
                if connection.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                    self._drop(ident)
                    return
                self._opened(ident, connection)
            if events & selectors.EVENT_WRITE and connection.pending:
                try:
                    count = connection.sock.send(connection.pending[:DATA_BYTES])
                except (BlockingIOError, InterruptedError):
                    count = 0
                if count:
                    del connection.pending[:count]
                    connection.available += count
                    self._queue(CREDIT, ident, struct.pack("!I", count))
                    self._finish(ident, connection)
            if ident not in self.connections:
                return
            if events & selectors.EVENT_READ:
                try:
                    data = connection.sock.recv(min(DATA_BYTES, connection.credit))
                except (BlockingIOError, InterruptedError):
                    return
                if data:
                    connection.credit -= len(data)
                    connection.sent += len(data)
                    connection.send_hash.update(data)
                    self.bytes_sent += len(data)
                    self._queue(DATA, ident, data)
                else:
                    connection.read_closed = True
                    self._queue(END, ident, struct.pack("!Q", connection.sent) + connection.send_hash.digest())
                    self._finish(ident, connection)
        except OSError:
            self._drop(ident)

    def run(self):
        for fd in (self.incoming, self.outgoing, self.stderr):
            if fd is not None:
                os.set_blocking(fd, False)
        with selectors.DefaultSelector() as self.selector:
            try:
                if self.remote:
                    self.is_ready = True
                    self._queue(READY, payload=json.dumps({"version": 1, "nonce": self.nonce, "ports": self.ports}).encode())
                    self.on_ready()
                self._monitor(self.incoming, selectors.EVENT_READ, ("input", None))
                if self.stderr is not None:
                    self._monitor(self.stderr, selectors.EVENT_READ, ("stderr", None))
                for port, listener in self.listeners.items():
                    listener.setblocking(False)
                    self._monitor(listener, selectors.EVENT_READ, ("listener", port))
                while not self.cancel.is_set():
                    now = time.monotonic()
                    if not self.check_active():
                        raise ProtocolError("Reverse ownership proof expired")
                    if now - self.last_received > self.heartbeat_timeout:
                        raise ProtocolError("Reverse channel heartbeat expired")
                    if now - self.last_ping >= self.heartbeat_interval:
                        self._queue(PING)
                        self.last_ping = now
                    self._monitor(self.outgoing, selectors.EVENT_WRITE if self.output else 0, ("output", None))
                    for ident, connection in list(self.connections.items()):
                        if connection.connecting and now >= connection.deadline:
                            self._drop(ident)
                            continue
                        events = selectors.EVENT_WRITE if connection.connecting or connection.pending else 0
                        if connection.accepted and not connection.read_closed and connection.credit:
                            events |= selectors.EVENT_READ
                        self._monitor(connection.sock, events, ("socket", ident))
                    for key, events in self.selector.select(.1):
                        kind, ident = key.data
                        if kind == "input":
                            self._read_channel()
                        elif kind == "output":
                            self._flush_channel()
                        elif kind == "listener":
                            self._accept(ident, key.fileobj)
                        elif kind == "socket":
                            self._socket(ident, events)
                        else:
                            try:
                                data = os.read(self.stderr, DATA_BYTES)
                            except (BlockingIOError, InterruptedError):
                                continue
                            if not data:
                                self._monitor(self.stderr, 0, None)
            finally:
                for ident in list(self.connections):
                    self._drop(ident, notify=False)
                for listener in self.listeners.values():
                    listener.close()


def remote_main():
    """Run without imports from the host package inside the restricted helper."""
    listeners = {}
    try:
        config = json.loads(sys.argv[1])
        address = ipaddress.ip_address(config["bind"])
        if address.version != 4 or address.is_unspecified or address.is_multicast:
            raise ValueError("Invalid listener address")
        ports = config["ports"]
        if (not isinstance(ports, list) or not 1 <= len(ports) <= 32 or len(set(ports)) != len(ports)
                or any(type(port) is not int or not 1024 <= port <= 65535 or port in (2375, 2376) for port in ports)):
            raise ValueError("Invalid reverse listener ports")
        for port in ports:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listeners[port] = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((str(address), port))
            listener.listen(MAX_CONNECTIONS)
        cancelled = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: cancelled.set())
        Peer(0, 1, nonce=config["nonce"], ports=ports, listeners=listeners, cancel=cancelled).run()
        return 0
    except (OSError, ValueError, KeyError, IndexError, TypeError, ProtocolError):
        print("Reverse helper stopped; existing TCP requests were not replayed", file=sys.stderr, flush=True)
        return 1
    finally:
        for listener in listeners.values():
            listener.close()


if __name__ == "__main__":
    raise SystemExit(remote_main())
