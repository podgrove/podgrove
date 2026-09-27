"""Real local sockets and child pipes exercise the Docker API stream bridge."""
from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest

from podgrove.docker_tunnel import BUFFER_BYTES, POD_UID_ENV, STDERR_BYTES, DockerTunnel
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube

IDENT = "012345abcdef"


class FakeKube:
    namespace = "default"

    def __init__(self, script):
        self.script = script
        self.commands = []
        self.reads = []
        labels = {MANAGED: "podgrove", ENVIRONMENT: IDENT}
        self.controller = {"metadata": {"name": f"pg-{IDENT}", "namespace": self.namespace,
                                         "uid": "controller-uid", "labels": labels.copy()}}
        self.pod = {"metadata": {"name": f"pg-{IDENT}-0", "namespace": self.namespace,
                                  "uid": "pod-uid", "labels": labels.copy(),
                                  "ownerReferences": [{"apiVersion": "apps/v1", "kind": "StatefulSet",
                                                       "name": f"pg-{IDENT}", "uid": "controller-uid", "controller": True}]},
                    "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": f"pg-{IDENT}"}}]}}

    def call(self, *args, **kwargs):
        self.reads.append(args)
        assert kwargs == {"timeout": 5}
        assert args[0] == "get" and args[1] in ("statefulset", "pod")
        return subprocess.CompletedProcess(args, 0, json.dumps(self.controller if args[1] == "statefulset" else self.pod), "")

    def command(self, *args):
        command = Kube("explicit-cluster", self.namespace).command(*args)
        self.commands.append(command)
        return [sys.executable, "-u", "-c", self.script]


class GuardedKube(FakeKube):
    def __init__(self, script):
        super().__init__(script)
        self.remote_uid = self.pod["metadata"]["uid"]
        self.pod["spec"]["containers"] = [{"name": "docker", "env": [{"name": POD_UID_ENV,
            "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}}}]}]

    def command(self, *args):
        self.commands.append(Kube("explicit-cluster", self.namespace).command(*args))
        remote = list(args[args.index("--") + 1:])
        assert remote[:2] == ["sh", "-c"]
        assert remote[-4:] == ["docker", "--host=unix:///var/run/docker.sock", "system", "dial-stdio"]
        # Run the real guard locally, substituting only its final dial-stdio
        # target with an inert Python process. No Kubernetes/Docker is contacted.
        return ["env", f"{POD_UID_ENV}={self.remote_uid}", *remote[:-4], sys.executable, "-u", "-c", self.script]


def receive(client):
    chunks = []
    while chunk := client.recv(65536):
        chunks.append(chunk)
    return b"".join(chunks)


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Timed out waiting for transport state")
        time.sleep(0.01)


def connect(tunnel):
    client = socket.create_connection(("127.0.0.1", tunnel.port), timeout=5)
    client.settimeout(5)
    return client


def test_stdin_eof_preserves_delayed_output_and_binary_bytes():
    kube = FakeKube("import sys,time; data=sys.stdin.buffer.read(); time.sleep(1.3); sys.stdout.buffer.write(data+b'\\x00after-eof')")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as client:
            client.sendall(b"payload\xff")
            client.shutdown(socket.SHUT_WR)
            started = time.monotonic()
            assert receive(client) == b"payload\xff\x00after-eof"
            assert time.monotonic() - started >= 1.3
        tunnel.check()
        command = kube.commands[0]
        assert command[:5] == ["kubectl", "--context", "explicit-cluster", "--namespace", "default"]
        assert command[-11:] == ["exec", "--request-timeout=0", "-i", f"pg-{IDENT}-0", "-c", "docker", "--",
                                "docker", "--host=unix:///var/run/docker.sock", "system", "dial-stdio"]
        assert "-t" not in command
    finally:
        tunnel.close()


def test_full_duplex_backpressure_larger_than_buffers():
    kube = FakeKube("import sys\nwhile chunk:=sys.stdin.buffer.read1(65536):\n sys.stdout.buffer.write(chunk);sys.stdout.buffer.flush()")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    payload = bytes(range(256)) * (BUFFER_BYTES // 256 * 8)
    try:
        with connect(tunnel) as client:
            errors = []
            def send():
                try:
                    client.sendall(payload)
                    client.shutdown(socket.SHUT_WR)
                except Exception as exc:
                    errors.append(exc)
            writer = threading.Thread(target=send)
            writer.start()
            assert receive(client) == payload
            writer.join(timeout=3)
            assert not writer.is_alive() and not errors
        tunnel.check()
    finally:
        tunnel.close()


def test_multiple_keepalive_requests_use_one_exec_stream():
    kube = FakeKube("import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(b'reply:'+line);sys.stdout.buffer.flush()")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as client:
            for message in (b"first\n", b"second\n", b"third\n"):
                client.sendall(message)
                assert client.recv(1024) == b"reply:" + message
            client.shutdown(socket.SHUT_WR)
            assert receive(client) == b""
        assert len(kube.commands) == 1
        tunnel.check()
    finally:
        tunnel.close()


@pytest.mark.parametrize("foreign", ["pod", "controller", "owner", "claim", "deleting", "missing"])
def test_foreign_or_missing_engine_refused_before_listener_or_exec(foreign):
    kube = FakeKube("raise AssertionError('must not execute')")
    if foreign == "pod":
        kube.pod["metadata"]["labels"][MANAGED] = "foreign"
    elif foreign == "controller":
        kube.controller["metadata"]["labels"][ENVIRONMENT] = "foreign"
    elif foreign == "owner":
        kube.pod["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    elif foreign == "claim":
        kube.pod["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "foreign"
    elif foreign == "deleting":
        kube.pod["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    else:
        kube.pod = {}
    tunnel = DockerTunnel(kube, IDENT, 0)
    with pytest.raises(PodgroveError):
        tunnel.start()
    assert not kube.commands and tunnel._listener is None


@pytest.mark.parametrize("replaced", ["pod", "controller"])
def test_new_connection_refuses_replacement_even_with_valid_labels(replaced):
    kube = FakeKube("raise AssertionError('must not execute')")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    if replaced == "pod":
        kube.pod["metadata"]["uid"] = "replacement"
    else:
        kube.controller["metadata"]["uid"] = "replacement"
        kube.pod["metadata"]["ownerReferences"][0]["uid"] = "replacement"
    try:
        with connect(tunnel) as client:
            with pytest.raises(ConnectionResetError):
                receive(client)
        with pytest.raises(PodgroveError, match="replaced"):
            tunnel.check()
        assert not kube.commands
    finally:
        tunnel.close()


def test_nonzero_transport_exit_resets_client_and_retains_bounded_error():
    kube = FakeKube("import sys;sys.stderr.write('x'*100000+'failure-marker');sys.stderr.flush();sys.exit(7)")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as client:
            with pytest.raises(ConnectionResetError):
                receive(client)
        with pytest.raises(PodgroveError, match="exited 7") as error:
            tunnel.check()
        assert "failure-marker" in str(error.value)
        assert len(str(error.value)) < STDERR_BYTES + 100
    finally:
        tunnel.close()


def test_session_close_reaps_children_and_threads_even_if_child_ignores_term():
    kube = FakeKube("import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('started',flush=True);time.sleep(60)")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    client = connect(tunnel)
    assert client.recv(1024) == b"started\n"
    streams = list(tunnel._streams.values())
    started = time.monotonic()
    tunnel.close()
    assert time.monotonic() - started < 4
    assert all(stream.process.poll() is not None and not stream.thread.is_alive() for stream in streams)
    assert not tunnel._streams and not tunnel._accept_thread.is_alive()
    assert client.recv(1024) == b""
    client.close()


def test_connection_limit_bounds_children_without_disrupting_existing_stream():
    kube = FakeKube("import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(line);sys.stdout.buffer.flush()")
    tunnel = DockerTunnel(kube, IDENT, 0, max_connections=1).start()
    try:
        with connect(tunnel) as first:
            first.sendall(b"ready\n")
            assert first.recv(1024) == b"ready\n"
            with connect(tunnel) as second:
                with pytest.raises(ConnectionResetError):
                    receive(second)
            first.sendall(b"still-connected\n")
            assert first.recv(1024) == b"still-connected\n"
            first.shutdown(socket.SHUT_WR)
            assert receive(first) == b""
        assert len(kube.commands) == 1
        tunnel.check()
    finally:
        tunnel.close()


def test_client_disconnect_does_not_leak_child():
    kube = FakeKube("import sys;sys.stdin.buffer.read()")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        client = connect(tunnel)
        wait_until(lambda: bool(kube.commands))
        client.close()
        wait_until(lambda: not tunnel._streams)
        tunnel.check()
    finally:
        tunnel.close()


def test_cancelled_client_does_not_disconnect_other_docker_clients():
    kube = FakeKube("import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        client = connect(tunnel)
        wait_until(lambda: bool(kube.commands))
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()
        wait_until(lambda: not tunnel._streams)
        tunnel.check()
        with connect(tunnel) as other:
            other.sendall(b"still-working")
            other.shutdown(socket.SHUT_WR)
            assert receive(other) == b"still-working"
        tunnel.check()
    finally:
        tunnel.close()


def test_independent_connections_preserve_distinct_payloads():
    kube = FakeKube("import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    results = {}
    def exchange(index):
        with connect(tunnel) as client:
            client.sendall(f"connection-{index}".encode())
            client.shutdown(socket.SHUT_WR)
            results[index] = receive(client)
    try:
        threads = [threading.Thread(target=exchange, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert results == {index: f"connection-{index}".encode() for index in range(4)}
        assert len(kube.commands) == 4
        tunnel.check()
    finally:
        tunnel.close()


def test_guarded_connections_avoid_repeated_gets_and_preserve_delayed_eof_output():
    kube = GuardedKube("import sys,time; data=sys.stdin.buffer.read(); time.sleep(1.3); print(data.decode()+'after-eof')")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        for _index in range(2):
            with connect(tunnel) as client:
                client.sendall(b"payload-")
                client.shutdown(socket.SHUT_WR)
                assert receive(client) == b"payload-after-eof\n"
        assert len(kube.reads) == 2  # Initial controller+Pod; each exec guards its UID.
        assert len(kube.commands) == 2
        tunnel.check()
    finally:
        tunnel.close()
    assert not tunnel._verification_thread.is_alive()


def test_guarded_replacement_uid_is_refused_inside_exec_before_daemon_access():
    kube = GuardedKube("print('UNSAFE daemon access')")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    kube.remote_uid = "replacement-pod-uid"
    try:
        with connect(tunnel) as client:
            client.shutdown(socket.SHUT_WR)
            with pytest.raises(ConnectionResetError):
                receive(client)
        with pytest.raises(PodgroveError, match="UID changed"):
            tunnel.check()
        assert len(kube.reads) == 2
    finally:
        tunnel.close()


@pytest.mark.parametrize("binding", [
    {"value": "pod-uid"},
    {"valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.name"}}},
    {"valueFrom": {"fieldRef": {"apiVersion": "v2", "fieldPath": "metadata.uid"}}},
])
def test_literal_or_wrong_uid_source_never_enables_cached_ownership(binding):
    kube = FakeKube("raise AssertionError('must not execute')")
    kube.pod["spec"]["containers"] = [{"name": "docker", "env": [{"name": POD_UID_ENV, **binding}]}]
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    kube.pod["metadata"]["uid"] = "replacement"
    try:
        assert tunnel._uid_guard is False and tunnel._verification_thread.is_alive()
        with connect(tunnel) as client:
            with pytest.raises(ConnectionResetError):
                receive(client)
        with pytest.raises(PodgroveError, match="replaced"):
            tunnel.check()
        assert not kube.commands
    finally:
        tunnel.close()


def test_background_revalidation_detects_label_drift_without_new_connections():
    kube = GuardedKube("print('UNSAFE daemon access')")
    tunnel = DockerTunnel(kube, IDENT, 0, verification_interval=0.05).start()
    kube.controller["metadata"]["labels"][MANAGED] = "foreign"
    try:
        wait_until(lambda: tunnel._error is not None)
        with pytest.raises(PodgroveError, match="foreign"):
            tunnel.check()
        assert not kube.commands
    finally:
        tunnel.close()


@pytest.mark.parametrize("kube_type", [FakeKube, GuardedKube], ids=["legacy", "uid-guarded"])
def test_ownership_failure_resets_existing_keepalive_streams_without_supervisor_poll(kube_type):
    kube = kube_type("import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(line);sys.stdout.buffer.flush()")
    tunnel = DockerTunnel(kube, IDENT, 0, verification_interval=0.05).start()
    clients = [connect(tunnel), connect(tunnel)]
    try:
        for client in clients:
            client.sendall(b"before-revocation\n")
            assert client.recv(1024) == b"before-revocation\n"
        streams = list(tunnel._streams.values())
        kube.controller["metadata"]["labels"][MANAGED] = "foreign"
        wait_until(lambda: tunnel._error is not None)
        original_error = tunnel._error
        # Do not call check()/close(): the tunnel must revoke active connections
        # even while its supervisor is blocked in an image build.
        for client in clients:
            with pytest.raises(ConnectionResetError):
                client.recv(1024)
        wait_until(lambda: not tunnel._streams)
        assert all(stream.process.poll() is not None and not stream.thread.is_alive() for stream in streams)
        assert tunnel._error == original_error and "foreign" in original_error
    finally:
        for client in clients:
            client.close()
        tunnel.close()


def test_transport_failure_aborts_other_streams_without_masking_original_error():
    kube = FakeKube("import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(line);sys.stdout.buffer.flush()")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as existing:
            existing.sendall(b"ready\n")
            assert existing.recv(1024) == b"ready\n"
            kube.script = "import sys;sys.stderr.write('primary-failure');sys.exit(7)"
            with connect(tunnel) as failing:
                with pytest.raises(ConnectionResetError):
                    failing.recv(1024)
            with pytest.raises(ConnectionResetError):
                existing.recv(1024)
        wait_until(lambda: not tunnel._streams)
        assert "exited 7" in tunnel._error and "primary-failure" in tunnel._error
    finally:
        tunnel.close()


def test_expired_guarded_cache_revalidates_before_opening_another_stream():
    kube = GuardedKube("print('UNSAFE daemon access')")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    tunnel._verified_at = time.monotonic() - 61
    kube.controller["metadata"]["uid"] = "replacement-controller"
    kube.pod["metadata"]["ownerReferences"][0]["uid"] = "replacement-controller"
    try:
        with connect(tunnel) as client:
            with pytest.raises(ConnectionResetError):
                receive(client)
        with pytest.raises(PodgroveError, match="replaced"):
            tunnel.check()
        assert not kube.commands
    finally:
        tunnel.close()
