"""Real sockets and a real remote helper exercise multiplexing and lifecycle."""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from podgrove import reverse_protocol as protocol
from podgrove.docker_tunnel import POD_UID_ENV, _UID_GUARD
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube
from podgrove.reverse import BRIDGE_FORMAT, IDENTITY_LABEL, INSPECT_FORMAT, OWNER_LABEL, ReverseForward, normalize_mappings

IDENT = "012345abcdef"
EXPECTED = {"statefulset_uid": "controller-uid", "pod_uid": "pod-uid"}


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("Reverse test condition did not become true")
        time.sleep(.01)


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def receive(sock):
    chunks = []
    while chunk := sock.recv(65536):
        chunks.append(chunk)
    return b"".join(chunks)


class SocketService:
    def __init__(self, handler, host="127.0.0.1"):
        self.listener = socket.socket(socket.AF_INET6 if host == "::1" else socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind((host, 0))
        self.listener.listen(64)
        self.listener.settimeout(.1)
        self.port = self.listener.getsockname()[1]
        self.handler = handler
        self.stopped = threading.Event()
        self.connections = []
        self.errors = []
        self.threads = []
        self.worker = threading.Thread(target=self._accept, daemon=True)
        self.worker.start()

    def _serve(self, client):
        try:
            self.handler(client)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception as exc:
            self.errors.append(exc)
        finally:
            client.close()

    def _accept(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            client.settimeout(5)
            self.connections.append(client)
            thread = threading.Thread(target=self._serve, args=(client,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def close(self):
        self.stopped.set()
        self.listener.close()
        for client in self.connections:
            with contextlib.suppress(OSError):
                client.shutdown(socket.SHUT_RDWR)
        self.worker.join(1)
        for thread in self.threads:
            thread.join(2)


class LocalKube:
    namespace = "owned-test"

    def __init__(self):
        self.commands = []
        self.controls = []
        self.reads = []
        self.containers = {}
        self.created = []
        self.removed = []
        self.remote_uid = EXPECTED["pod_uid"]
        self.unavailable = False
        self.block_read = None
        self.read_started = threading.Event()
        self.read_cancelled = threading.Event()
        self.creation_response = None
        self.block_create = None
        self.create_started = threading.Event()
        self.start_script = None
        self.bridge_config = [{"Gateway": "172.17.0.1", "Subnet": "172.17.0.0/16"}]
        self.labels = {MANAGED: "podgrove", ENVIRONMENT: IDENT}
        self.controller = {"metadata": {"name": f"pg-{IDENT}", "namespace": self.namespace,
                                         "uid": EXPECTED["statefulset_uid"], "labels": dict(self.labels)}}
        self.pod = {"metadata": {"name": f"pg-{IDENT}-0", "namespace": self.namespace,
                                  "uid": EXPECTED["pod_uid"], "labels": dict(self.labels),
                                  "ownerReferences": [{"controller": True, "apiVersion": "apps/v1",
                                                       "kind": "StatefulSet", "name": f"pg-{IDENT}",
                                                       "uid": EXPECTED["statefulset_uid"]}]}}

    def call(self, *args, **kwargs):
        if args[0] == "get":
            self.reads.append(args)
            assert args[1] in ("statefulset", "pod")
            assert kwargs["timeout"] <= 15
            self.read_started.set()
            if self.block_read is not None:
                deadline = time.monotonic() + kwargs["timeout"]
                while not self.block_read.wait(.01):
                    if kwargs["cancel_event"].is_set():
                        self.read_cancelled.set()
                        self.block_read = None
                        raise PodgroveError("cancelled")
                    if time.monotonic() >= deadline:
                        raise PodgroveError("timeout")
            if self.unavailable:
                return subprocess.CompletedProcess(args, 1, "", "unavailable")
            value = self.controller if args[1] == "statefulset" else self.pod
            return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
        self.controls.append(args)
        assert args[:7] == ("exec", "--request-timeout=0", "-i", f"pg-{IDENT}-0", "-c", "docker", "--")
        remote = args[7:]
        assert remote[:5] == ("sh", "-c", _UID_GUARD, "podgrove-reverse-guard", EXPECTED["pod_uid"])
        assert remote[5:7] == ("docker", "--host=unix:///var/run/docker.sock")
        assert kwargs["env"]["KUBECTL_REMOTE_COMMAND_WEBSOCKETS"] == "true"
        action = remote[7:]
        result = ""
        if action[:2] == ("network", "inspect"):
            assert action[2:] == ("--format", BRIDGE_FORMAT, "bridge")
            result = json.dumps({"Name": "bridge", "Driver": "bridge", "IPAM": {
                "Config": self.bridge_config}})
        elif action[0] == "create":
            self.create_started.set()
            if self.block_create is not None:
                while not self.block_create.wait(.01):
                    if kwargs["cancel_event"].is_set():
                        raise PodgroveError("cancelled")
            ident = f"{len(self.created) + 1:064x}"
            name = action[action.index("--name") + 1]
            labels = dict(value.split("=", 1) for key, value in zip(action, action[1:]) if key == "--label")
            config = json.loads(action[-1])
            value = {"Id": ident, "Name": "/" + name, "Config": {"Labels": labels}, "test_config": config}
            self.containers[ident] = value
            self.created.append(action)
            result = self.creation_response if self.creation_response is not None else ident + "\n"
        elif action[:2] == ("container", "inspect"):
            assert action[2:4] == ("--format", INSPECT_FORMAT)
            value = next((value for key, value in self.containers.items()
                          if key == action[4] or value["Name"] == "/" + action[4]), None)
            if value is None:
                return subprocess.CompletedProcess(args, 1, "", "Error: No such container")
            result = json.dumps({"Id": value["Id"], "Name": value["Name"], "Labels": value["Config"]["Labels"]})
        elif action[0] == "rm":
            assert action[1] == "--force" and len(action[2]) == 64
            self.removed.append(action[2])
            self.containers.pop(action[2], None)
        else:
            raise AssertionError(action)
        return subprocess.CompletedProcess(args, 0, result, "")

    def command(self, *args):
        self.commands.append(Kube("explicit-context", self.namespace).command(*args))
        ident = args[-1]
        config = self.containers[ident]["test_config"]
        target = ([sys.executable, "-I", "-S", "-B", "-u", "-c", self.start_script]
                  if self.start_script is not None else
                  [sys.executable, "-I", "-S", "-B", "-u", protocol.__file__, json.dumps(config)])
        return ["env", f"{POD_UID_ENV}={self.remote_uid}", "sh", "-c", _UID_GUARD,
                "podgrove-reverse-guard", EXPECTED["pod_uid"], *target]


class LocalReverse(ReverseForward):
    def _gateway(self):
        assert super()._gateway() == "172.17.0.1"
        return "127.0.0.1"


@pytest.fixture
def services():
    values = []
    def create(handler, **kwargs):
        value = SocketService(handler, **kwargs)
        values.append(value)
        return value
    yield create
    for value in values:
        value.close()
        assert not value.errors


@pytest.fixture
def reverse():
    values = []
    def create(port, *, kube=None, remote_port=None, **kwargs):
        value = LocalReverse(kube or LocalKube(), IDENT,
                             [{"local_port": port, "remote_port": remote_port or unused_port()}], EXPECTED,
                             startup_timeout=8, **kwargs)
        values.append(value)
        value.start()
        return value
    yield create
    for value in values:
        value.close()


def dial(tunnel):
    client = socket.create_connection(("127.0.0.1", tunnel.mappings[0]["remote_port"]), timeout=5)
    client.settimeout(5)
    return client


def echo(client):
    while data := client.recv(65536):
        client.sendall(data)


def test_full_duplex_binary_stream_larger_than_windows(reverse, services):
    service = services(echo)
    tunnel = reverse(service.port)
    payload = bytes(range(256)) * (protocol.WINDOW_BYTES // 256 * 12)
    with dial(tunnel) as client:
        errors = []
        def send():
            try:
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=send)
        thread.start()
        assert receive(client) == payload
        thread.join(3)
        assert not thread.is_alive() and not errors
    state = tunnel.snapshot()
    assert state["state"] == "ready"
    assert state["connection_buffer_peak"] <= protocol.WINDOW_BYTES
    assert state["output_buffer_peak"] <= protocol.OUTPUT_BYTES


def test_remote_half_close_keeps_delayed_local_response(reverse, services):
    def after_eof(client):
        request = receive(client)
        time.sleep(.2)
        client.sendall(b"after-eof:" + request + b"\xff\x00")
    tunnel = reverse(services(after_eof).port)
    with dial(tunnel) as client:
        client.sendall(b"request")
        client.shutdown(socket.SHUT_WR)
        assert receive(client) == b"after-eof:request\xff\x00"


def test_local_half_close_preserves_reverse_input(reverse, services):
    received = []
    def greeting(client):
        client.sendall(b"greeting")
        client.shutdown(socket.SHUT_WR)
        received.append(receive(client))
    tunnel = reverse(services(greeting).port)
    with dial(tunnel) as client:
        assert receive(client) == b"greeting"
        client.sendall(b"after-the-other-end-half-closed")
        client.shutdown(socket.SHUT_WR)
    wait_for(lambda: received)
    assert received == [b"after-the-other-end-half-closed"]


def test_real_http_keepalive_and_binary_post(reverse):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        tunnel = reverse(server.server_port)
        connection = http.client.HTTPConnection("127.0.0.1", tunnel.mappings[0]["remote_port"], timeout=5)
        for body in (b"first", bytes(range(256)) * 4096, b"third"):
            connection.request("POST", "/stream", body=body)
            response = connection.getresponse()
            assert response.status == 200 and response.read() == body
        connection.close()
        assert len(tunnel.kube.commands) == 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_refused_local_target_does_not_poison_other_connections(reverse, services):
    port = unused_port()
    tunnel = reverse(port)
    with dial(tunnel) as client:
        with pytest.raises((ConnectionResetError, BrokenPipeError)):
            client.sendall(b"not-replayed")
            while client.recv(100):
                pass
    assert tunnel.snapshot()["state"] == "ready"
    service = services(echo)
    tunnel.mappings[0]["local_port"] = service.port
    tunnel._peer.targets[tunnel.mappings[0]["remote_port"]] = ("127.0.0.1", service.port)
    with dial(tunnel) as client:
        client.sendall(b"works")
        assert client.recv(5) == b"works"


def test_stalled_connection_does_not_block_second_stream_or_close(reverse, services):
    stalled = threading.Event()
    release = threading.Event()
    def handler(client):
        initial = client.recv(1)
        if initial == b"S":
            stalled.set()
            release.wait(5)
        else:
            client.sendall(initial)
            echo(client)
    tunnel = reverse(services(handler).port)
    client = dial(tunnel)
    writer = None
    try:
        client.sendall(b"S")
        assert stalled.wait(2)
        def flood():
            try:
                for _ in range(1024):
                    client.sendall(b"x" * 65536)
            except OSError:
                pass
        writer = threading.Thread(target=flood)
        writer.start()
        with dial(tunnel) as other:
            other.sendall(b"ping")
            assert other.recv(1) == b"p"
            assert other.recv(3) == b"ing"
        started = time.monotonic()
        tunnel.close()
        assert time.monotonic() - started < 3
        writer.join(2)
        assert not writer.is_alive()
        assert tunnel.snapshot()["connection_buffer_peak"] <= protocol.WINDOW_BYTES
    finally:
        release.set()
        client.close()
        if writer is not None:
            writer.join(2)


def test_channel_recovery_keeps_ports_and_never_replays_tcp(reverse, services):
    seen = []
    def record(client):
        while data := client.recv(65536):
            seen.append(data)
            client.sendall(data)
    tunnel = reverse(services(record).port)
    first_id = tunnel._container_id
    client = dial(tunnel)
    client.sendall(b"old-request")
    assert client.recv(11) == b"old-request"
    os.kill(tunnel._process.pid, signal.SIGTERM)
    wait_for(lambda: tunnel._container_id != first_id and tunnel.snapshot()["state"] == "ready")
    with pytest.raises(ConnectionResetError):
        client.recv(1)
    client.close()
    with dial(tunnel) as fresh:
        fresh.sendall(b"new-request")
        assert fresh.recv(11) == b"new-request"
    assert b"".join(seen) == b"old-requestnew-request"
    assert first_id in tunnel.kube.removed
    assert tunnel.snapshot()["attempts"] == 1


@pytest.mark.parametrize("field", ["pod", "statefulset"])
def test_engine_replacement_closes_live_stream_and_refuses_new_helper(reverse, services, field):
    kube = LocalKube()
    tunnel = reverse(services(echo).port, kube=kube, verification_interval=.05)
    with dial(tunnel) as client:
        client.sendall(b"before")
        assert client.recv(6) == b"before"
        target = kube.pod if field == "pod" else kube.controller
        target["metadata"]["uid"] = "replacement"
        wait_for(lambda: tunnel.snapshot()["state"] == "disconnected")
        with pytest.raises(PodgroveError, match="ownership changed"):
            tunnel.check()
        assert len(kube.created) == 1


def test_ownership_outage_gates_new_connections_and_recovers_same_channel(reverse, services):
    kube = LocalKube()
    tunnel = reverse(services(echo).port, kube=kube, verification_interval=.05)
    original = tunnel._container_id
    with dial(tunnel) as old:
        old.sendall(b"first")
        assert old.recv(5) == b"first"
        kube.unavailable = True
        wait_for(lambda: tunnel.snapshot()["verification"] == "unavailable")
        old.sendall(b"still-alive")
        assert old.recv(11) == b"still-alive"
        with dial(tunnel) as rejected:
            with pytest.raises(ConnectionResetError):
                rejected.recv(1)
        kube.unavailable = False
        wait_for(lambda: tunnel.snapshot()["state"] == "ready")
        assert tunnel._container_id == original


def test_close_cancels_blocked_ownership_read(reverse, services):
    kube = LocalKube()
    tunnel = reverse(services(echo).port, kube=kube, verification_interval=.05)
    kube.read_started.clear()
    kube.block_read = threading.Event()
    assert kube.read_started.wait(2)
    started = time.monotonic()
    tunnel.close()
    assert time.monotonic() - started < 3
    assert kube.read_cancelled.is_set()
    assert not tunnel._monitor.is_alive() and not tunnel._worker.is_alive()
    tunnel.close()


def test_helper_has_restricted_flags_and_cleanup_uses_only_captured_id(reverse, services):
    tunnel = reverse(services(echo).port)
    command = tunnel.kube.created[0]
    assert command[command.index("--network") + 1] == "host"
    assert command[command.index("--user") + 1] == "65534:65534"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert "--read-only" in command and "--rm" in command
    assert not any(value in command for value in ("--mount", "--volume", "-v", "--privileged", "--publish"))
    ident = tunnel._container_id
    tunnel.close()
    assert tunnel.kube.removed == [ident]
    assert all(call[0] == "get" and call[1] in ("pod", "statefulset") for call in tunnel.kube.reads)
    actual = tunnel.kube.commands[0]
    assert actual[:5] == ["kubectl", "--context", "explicit-context", "--namespace", "owned-test"]


@pytest.mark.parametrize("mapping", [
    {"local_port": True}, {"local_port": 0}, {"local_port": 65536},
    {"local_port": 80}, {"local_port": 9000, "remote_port": 2375},
    {"local_port": 9000, "remote_port": 2376}, {"local_port": 9000, "local_host": "localhost"},
    {"local_port": 9000, "local_host": "0.0.0.0"}, {"local_port": 9000, "local_host": "10.0.0.1"},
    {"local_port": 9000, "host": "127.0.0.1"},
])
def test_invalid_mapping_refused_before_any_external_operation(mapping):
    with pytest.raises(PodgroveError):
        normalize_mappings([mapping])


def test_mapping_defaults_explicit_low_local_port_and_duplicate_remote():
    assert normalize_mappings([{"local_port": 8000}]) == [{"local_host": "127.0.0.1", "local_port": 8000, "remote_port": 8000}]
    assert normalize_mappings([{"local_port": 80, "remote_port": 8000, "local_host": "::1"}])[0]["local_port"] == 80
    with pytest.raises(PodgroveError, match="duplicate"):
        normalize_mappings([{"local_port": 8000}, {"local_port": 80, "remote_port": 8000}])


def test_protocol_rejects_oversized_frame_and_invalid_completion():
    read, write = os.pipe()
    try:
        peer = protocol.Peer(read, write, nonce="a" * 32, ports=[8000], targets={8000: ("127.0.0.1", 80)})
        os.write(write, protocol.HEADER.pack(protocol.DATA, 1, protocol.DATA_BYTES + 1))
        with pytest.raises(protocol.ProtocolError, match="size limit"):
            peer._read_channel()
        left, right = socket.socketpair()
        try:
            peer.is_ready = True
            peer.high_id = 1
            peer.connections[1] = protocol.Connection(left, accepted=True)
            with pytest.raises(protocol.ProtocolError, match="completion"):
                peer._frame(protocol.END, 1, b"\x00" * 40)
            assert peer.connections[1].received_end is False
        finally:
            left.close()
            right.close()
    finally:
        os.close(read)
        os.close(write)


def test_protocol_window_refuses_uncredited_data():
    left, right = socket.socketpair()
    try:
        peer = protocol.Peer(0, 1, nonce="a" * 32, ports=[8000], targets={8000: ("127.0.0.1", 80)})
        peer.is_ready = True
        peer.high_id = 1
        connection = protocol.Connection(left, accepted=True)
        peer.connections[1] = connection
        for _ in range(protocol.WINDOW_BYTES // protocol.DATA_BYTES):
            peer._frame(protocol.DATA, 1, b"x" * protocol.DATA_BYTES)
        assert len(connection.pending) == protocol.WINDOW_BYTES
        with pytest.raises(protocol.ProtocolError, match="receive window"):
            peer._frame(protocol.DATA, 1, b"x")
        assert len(connection.pending) == protocol.WINDOW_BYTES
    finally:
        left.close()
        right.close()


def test_multiple_listeners_route_to_distinct_loopback_targets(services):
    def first(client):
        client.sendall(b"first:" + client.recv(10))
    def second(client):
        client.sendall(b"second:" + client.recv(10))
    local = [services(first).port, services(second).port]
    remote = [unused_port(), unused_port()]
    tunnel = LocalReverse(LocalKube(), IDENT,
                          [{"local_port": port, "remote_port": listener} for port, listener in zip(local, remote)],
                          EXPECTED, startup_timeout=8).start()
    try:
        for index, port in enumerate(remote):
            with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
                client.sendall(b"request")
                client.shutdown(socket.SHUT_WR)
                assert receive(client) == (b"first:" if index == 0 else b"second:") + b"request"
        assert len(tunnel.kube.commands) == 1
    finally:
        tunnel.close()


def test_literal_ipv6_local_target_uses_no_name_lookup(services, monkeypatch):
    service = services(echo, host="::1")
    tunnel = LocalReverse(LocalKube(), IDENT,
                          [{"local_port": service.port, "remote_port": unused_port(), "local_host": "::1"}],
                          EXPECTED, startup_timeout=8).start()
    try:
        def no_lookup(*_args, **_kwargs):
            raise AssertionError("Reverse targets must not use DNS")
        monkeypatch.setattr(socket, "getaddrinfo", no_lookup)
        with socket.socket() as client:
            client.settimeout(3)
            client.connect(("127.0.0.1", tunnel.mappings[0]["remote_port"]))
            client.sendall(b"ipv6")
            client.shutdown(socket.SHUT_WR)
            assert receive(client) == b"ipv6"
    finally:
        tunnel.close()


def test_connection_limit_is_bounded_and_existing_streams_survive(reverse, services):
    tunnel = reverse(services(echo).port)
    clients = []
    try:
        for index in range(protocol.MAX_CONNECTIONS):
            client = dial(tunnel)
            clients.append(client)
            payload = bytes([index])
            client.sendall(payload)
            assert client.recv(1) == payload
        with dial(tunnel) as extra:
            with pytest.raises(ConnectionResetError):
                extra.recv(1)
        assert tunnel.snapshot()["active_connections"] == protocol.MAX_CONNECTIONS
        clients[0].sendall(b"survives")
        assert clients[0].recv(8) == b"survives"
    finally:
        for client in clients:
            client.close()


def test_unknown_create_response_cleans_only_nonce_proven_container(monkeypatch):
    monkeypatch.setattr("podgrove.reverse.RETRY_DELAYS", ())
    kube = LocalKube()
    kube.creation_response = "--arbitrary-not-a-container-id"
    tunnel = LocalReverse(kube, IDENT, [{"local_port": 8000, "remote_port": unused_port()}], EXPECTED,
                          startup_timeout=3)
    with pytest.raises(PodgroveError):
        tunnel.start()
    assert kube.removed == [f"{1:064x}"]
    assert not kube.commands
    assert all("--arbitrary-not-a-container-id" not in operation for operation in kube.controls)
    assert tunnel.snapshot()["cleanup_error"] is None


def test_helper_label_change_refuses_cleanup_and_recovery(reverse, services):
    tunnel = reverse(services(echo).port)
    ident = tunnel._container_id
    tunnel.kube.containers[ident]["Config"]["Labels"][OWNER_LABEL] = "different-owner"
    os.kill(tunnel._process.pid, signal.SIGTERM)
    wait_for(lambda: tunnel.snapshot()["state"] == "disconnected")
    with pytest.raises(PodgroveError, match="ownership changed"):
        tunnel.check()
    assert ident not in tunnel.kube.removed
    assert len(tunnel.kube.created) == 1
    assert tunnel.snapshot()["cleanup_error"]


def test_name_replacement_is_not_removed_by_immutable_cleanup(reverse, services):
    tunnel = reverse(services(echo).port)
    old = tunnel._container_id
    replacement = "f" * 64
    value = dict(tunnel.kube.containers.pop(old), Id=replacement)
    assert value["Config"]["Labels"][IDENTITY_LABEL] == IDENT
    tunnel.kube.containers[replacement] = value
    tunnel.close()
    assert replacement in tunnel.kube.containers
    assert replacement not in tunnel.kube.removed


def test_bind_failure_is_bounded_and_does_not_take_over_listener(monkeypatch, services):
    monkeypatch.setattr("podgrove.reverse.RETRY_DELAYS", (.01, .01))
    service = services(echo)
    kube = LocalKube()
    tunnel = LocalReverse(kube, IDENT, [{"local_port": 8000, "remote_port": service.port}], EXPECTED,
                          startup_timeout=5)
    with pytest.raises(PodgroveError, match="exhausted"):
        tunnel.start()
    assert len(kube.created) == 3 and len(kube.removed) == 3
    with socket.create_connection(("127.0.0.1", service.port), timeout=2) as client:
        client.sendall(b"original listener")
        assert client.recv(17) == b"original listener"


def test_real_exec_uid_guard_refuses_changed_pod_before_listener(monkeypatch):
    monkeypatch.setattr("podgrove.reverse.RETRY_DELAYS", ())
    kube = LocalKube()
    kube.remote_uid = "different-pod"
    port = unused_port()
    tunnel = LocalReverse(kube, IDENT, [{"local_port": 8000, "remote_port": port}], EXPECTED, startup_timeout=3)
    with pytest.raises(PodgroveError):
        tunnel.start()
    assert len(kube.commands) == 1
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_ownership_age_expires_while_verification_is_blocked(reverse, services, monkeypatch):
    monkeypatch.setattr("podgrove.reverse.RETRY_DELAYS", ())
    kube = LocalKube()
    tunnel = reverse(services(echo).port, kube=kube, verification_interval=.05, max_verification_age=.3)
    with dial(tunnel) as client:
        client.sendall(b"before")
        assert client.recv(6) == b"before"
        kube.read_started.clear()
        kube.block_read = threading.Event()
        assert kube.read_started.wait(2)
        with pytest.raises(ConnectionResetError):
            client.recv(1)
        assert len(kube.created) == 1
        tunnel.close()
        assert kube.read_cancelled.is_set()


@pytest.mark.parametrize("gateway", ["0.0.0.0", "127.0.0.1", "169.254.1.1", "8.8.8.8", "224.0.0.1"])
def test_gateway_never_expands_to_public_or_wildcard_listener(gateway):
    kube = LocalKube()
    kube.bridge_config = [{"Gateway": gateway, "Subnet": "0.0.0.0/0"}]
    tunnel = ReverseForward(kube, IDENT, [{"local_port": 8000}], EXPECTED)
    with pytest.raises(PodgroveError, match="private default-bridge gateway"):
        tunnel._gateway()
    assert not kube.created


def test_dual_stack_bridge_uses_ipv4_gateway_without_requiring_ipv6_gateway():
    kube = LocalKube()
    kube.bridge_config.append({"Subnet": "fd00:abcd::/64"})
    tunnel = ReverseForward(kube, IDENT, [{"local_port": 8000}], EXPECTED)
    assert tunnel._gateway() == "172.17.0.1"


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf"), True, "1"])
def test_lifecycle_budgets_are_finite_positive_numbers(budget):
    with pytest.raises(ValueError, match="budgets"):
        ReverseForward(LocalKube(), IDENT, [{"local_port": 8000}], EXPECTED, startup_timeout=budget)


def test_closed_before_start_cannot_launch_a_helper():
    kube = LocalKube()
    tunnel = ReverseForward(kube, IDENT, [{"local_port": 8000}], EXPECTED)
    tunnel.close()
    tunnel.close()
    with pytest.raises(PodgroveError, match="closed"):
        tunnel.start()
    assert not kube.controls and not kube.reads


def test_ready_frame_accepts_arbitrary_transport_chunk_boundaries():
    read, write = os.pipe()
    calls = []
    try:
        peer = protocol.Peer(read, write, nonce="a" * 32, ports=[8000], targets={8000: ("127.0.0.1", 80)},
                             ready=lambda: calls.append(True))
        payload = json.dumps({"version": 1, "nonce": "a" * 32, "ports": [8000]}).encode()
        frame = protocol.HEADER.pack(protocol.READY, 0, len(payload)) + payload
        for byte in frame:
            os.write(write, bytes([byte]))
            peer._read_channel()
        assert calls == [True] and peer.is_ready and not peer.input_buffer
    finally:
        os.close(read)
        os.close(write)


def test_partial_frame_eof_never_becomes_successful_completion():
    read, write = os.pipe()
    try:
        peer = protocol.Peer(read, 1, nonce="a" * 32, ports=[8000], targets={8000: ("127.0.0.1", 80)})
        os.write(write, protocol.HEADER.pack(protocol.READY, 0, 100) + b"partial")
        peer._read_channel()
        os.close(write)
        write = None
        with pytest.raises(protocol.ProtocolError, match="closed"):
            peer._read_channel()
        assert not peer.is_ready
    finally:
        os.close(read)
        if write is not None:
            os.close(write)


def test_end_checksum_covers_exact_count_and_binary_bytes():
    left, right = socket.socketpair()
    try:
        peer = protocol.Peer(0, 1, nonce="a" * 32, ports=[8000], targets={8000: ("127.0.0.1", 80)})
        peer.is_ready = True
        peer.high_id = 1
        connection = protocol.Connection(left, accepted=True)
        peer.connections[1] = connection
        payload = b"\x00\xffbinary\r\n"
        peer._frame(protocol.DATA, 1, payload)
        for proof in ((len(payload) - 1).to_bytes(8, "big") + hashlib.sha256(payload).digest(),
                      len(payload).to_bytes(8, "big") + hashlib.sha256(payload + b"x").digest()):
            with pytest.raises(protocol.ProtocolError, match="completion"):
                peer._frame(protocol.END, 1, proof)
        assert not connection.received_end and not connection.write_closed
        peer._frame(protocol.END, 1, len(payload).to_bytes(8, "big") + hashlib.sha256(payload).digest())
        assert connection.received_end and not connection.write_closed
        assert connection.pending == payload
    finally:
        left.close()
        right.close()


def test_missing_heartbeat_stops_actual_selector_loop_promptly():
    incoming, remote_write = os.pipe()
    remote_read, outgoing = os.pipe()
    try:
        peer = protocol.Peer(incoming, outgoing, nonce="a" * 32, ports=[8000],
                             targets={8000: ("127.0.0.1", 8000)}, heartbeat_interval=.05, heartbeat_timeout=.2)
        started = time.monotonic()
        with pytest.raises(protocol.ProtocolError, match="heartbeat expired"):
            peer.run()
        assert time.monotonic() - started < 1
        assert not peer.connections
    finally:
        for fd in (incoming, remote_write, remote_read, outgoing):
            os.close(fd)


@pytest.mark.parametrize("phase", ["create", "ready"])
def test_public_cancel_wakes_startup_and_reaps_owned_operations(phase):
    kube = LocalKube()
    if phase == "create":
        kube.block_create = threading.Event()
    else:
        kube.start_script = "import time;time.sleep(30)"
    tunnel = LocalReverse(kube, IDENT, [{"local_port": 8000, "remote_port": unused_port()}], EXPECTED,
                          startup_timeout=30)
    errors, successes = [], []
    def start():
        try:
            successes.append(tunnel.start())
        except PodgroveError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=start)
    thread.start()
    process = None
    try:
        if phase == "create":
            assert kube.create_started.wait(2)
        else:
            wait_for(lambda: tunnel._process is not None)
            process = tunnel._process
        started = time.monotonic()
        tunnel.cancel()
        assert time.monotonic() - started < .1
        thread.join(4)
        assert not thread.is_alive() and not successes
        assert errors and "cancelled" in errors[0]
        assert not tunnel._worker.is_alive() and not tunnel._monitor.is_alive()
        if process is not None:
            assert process.poll() is not None and kube.removed == [f"{1:064x}"]
        else:
            assert not kube.created and not kube.commands
            assert tunnel.snapshot()["cleanup_error"] and tunnel.snapshot()["helper_name"]
    finally:
        tunnel.close()
        thread.join(4)


def test_late_ready_after_cancellation_cannot_mark_startup_success():
    tunnel = ReverseForward(LocalKube(), IDENT, [{"local_port": 8000}], EXPECTED)
    tunnel.cancel()
    with pytest.raises(PodgroveError, match="cancelled"):
        tunnel._ready()
    assert tunnel.snapshot()["state"] != "ready"


def test_uncertain_create_absence_retains_nonce_and_refuses_another_helper():
    kube = LocalKube()
    tunnel = LocalReverse(kube, IDENT, [{"local_port": 8000}], EXPECTED)
    tunnel._name = "podgrove-reverse-" + IDENT + "-" + "a" * 32
    tunnel._nonce = "a" * 32
    tunnel._creation_uncertain = True
    original = tunnel._name
    with pytest.raises(PodgroveError, match="creation outcome is still uncertain"):
        tunnel._cleanup()
    assert tunnel._name == original and not kube.created and not kube.removed
    ident = "f" * 64
    kube.containers[ident] = {"Id": ident, "Name": "/" + original,
                             "Config": {"Labels": {IDENTITY_LABEL: IDENT, OWNER_LABEL: "a" * 32}}}
    tunnel._cleanup()
    assert kube.removed == [ident] and tunnel._name is None and not tunnel._creation_uncertain


def test_cleanup_requires_observed_absence_even_when_rm_reports_success(monkeypatch):
    kube = LocalKube()
    tunnel = LocalReverse(kube, IDENT, [{"local_port": 8000}], EXPECTED)
    tunnel._create(tunnel._gateway())
    ident = tunnel._container_id
    original = kube.call
    def call(*args, **kwargs):
        if "rm" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        return original(*args, **kwargs)
    monkeypatch.setattr(kube, "call", call)
    with pytest.raises(PodgroveError, match="still exists after cleanup"):
        tunnel._cleanup()
    assert tunnel._container_id == ident and ident in kube.containers
    monkeypatch.setattr(kube, "call", original)
    tunnel._cleanup()
    assert tunnel._container_id is None and ident not in kube.containers


def test_inspection_requests_only_ownership_metadata_without_echoing_program():
    kube = LocalKube()
    tunnel = LocalReverse(kube, IDENT, [{"local_port": 8000}], EXPECTED)
    tunnel._create(tunnel._gateway())
    ident = tunnel._container_id
    kube.containers[ident]["Config"]["Cmd"] = ["python", "-c", "private program body" * 10000]
    assert tunnel._inspect(ident) == ident
    commands = [call for call in kube.controls if "container" in call]
    assert commands and all(call[-3:] == ("--format", INSPECT_FORMAT, ident) for call in commands)
    assert ".Cmd" not in INSPECT_FORMAT and ".State" not in INSPECT_FORMAT
    tunnel._cleanup()


@pytest.mark.parametrize("failure,reason,code", [
    ("timeout", "timeout", None), ("permission", "permission_denied", None), ("exit", "command_exit", 126),
])
def test_cold_inspect_failure_retains_fixed_stage_reason_without_subprocess_content(monkeypatch, failure, reason, code):
    monkeypatch.setattr("podgrove.reverse.RETRY_DELAYS", ())
    kube = LocalKube()
    original = kube.call
    secret = "PRIVATE-CREDENTIAL=never-show-subprocess-content"
    def call(*args, **kwargs):
        if "container" in args and "inspect" in args:
            if failure == "timeout":
                raise subprocess.TimeoutExpired([secret], 10, output=secret, stderr=secret)
            if failure == "permission":
                raise PermissionError(secret)
            return subprocess.CompletedProcess(args, 126, secret, secret)
        return original(*args, **kwargs)
    monkeypatch.setattr(kube, "call", call)
    tunnel = LocalReverse(kube, IDENT, [{"local_port": 8000, "remote_port": unused_port()}], EXPECTED, startup_timeout=4)
    with pytest.raises(PodgroveError, match="inspect_helper") as error:
        tunnel.start()
    snapshot = tunnel.snapshot()
    expected = {"stage": "inspect_helper", "reason": reason, "attempt": 0}
    if code is not None:
        expected["returncode"] = code
    assert snapshot["first_failure"] == expected
    assert snapshot["last_failure"] == expected
    assert snapshot["cleanup_failure"]["stage"] == "cleanup_inspect_helper"
    assert secret not in json.dumps(snapshot) and secret not in str(error.value)
    assert snapshot["helper_id"] and not kube.commands


def test_bridge_inspection_excludes_attached_container_inventory():
    kube = LocalKube()
    tunnel = ReverseForward(kube, IDENT, [{"local_port": 8000}], EXPECTED)
    assert tunnel._gateway() == "172.17.0.1"
    command, = kube.controls
    assert command[-5:] == ("network", "inspect", "--format", BRIDGE_FORMAT, "bridge")
    assert BRIDGE_FORMAT == '{"Name":{{json .Name}},"Driver":{{json .Driver}},"IPAM":{"Config":{{json .IPAM.Config}}}}'
    assert ".Containers" not in BRIDGE_FORMAT and ".Options" not in BRIDGE_FORMAT


@pytest.mark.parametrize("metadata", [
    {"Name": "other", "Driver": "bridge", "IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]}},
    {"Name": "bridge", "Driver": "overlay", "IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"}]}},
    {"Name": "bridge", "Driver": "bridge", "IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.18.0.1"}]}},
    {"Name": "bridge", "Driver": "bridge", "IPAM": {"Config": [{"Subnet": "172.17.0.0/16", "Gateway": "172.17.0.1"},
                                                                  {"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"}]}},
    {"Name": "bridge", "Driver": "bridge", "IPAM": {"Config": None}},
    [],
])
def test_compact_bridge_metadata_preserves_identity_subnet_and_ambiguity_refusal(monkeypatch, metadata):
    kube = LocalKube()
    monkeypatch.setattr(kube, "call", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(metadata), ""))
    tunnel = ReverseForward(kube, IDENT, [{"local_port": 8000}], EXPECTED)
    with pytest.raises(PodgroveError, match="private default-bridge gateway"):
        tunnel._gateway()
