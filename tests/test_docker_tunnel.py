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

from podgrove.docker_tunnel import BUFFER_BYTES, POD_UID_ENV, DockerTunnel
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, REQUEST_PROCESS_TIMEOUT, Kube

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
        assert 0 < kwargs["timeout"] <= REQUEST_PROCESS_TIMEOUT
        assert kwargs["check"] is False and callable(getattr(kwargs["cancel_event"], "is_set", None))
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


def receive_exact(client, size):
    """TCP may split a marker across reads; retain the socket's finite timeout."""
    data = bytearray()
    while len(data) < size:
        chunk = client.recv(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


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
                assert receive_exact(client, len(b"reply:" + message)) == b"reply:" + message
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


def test_nonzero_transport_exit_resets_only_client_and_retains_safe_diagnostics(caplog):
    kube = FakeKube("import sys;sys.stderr.write('x'*100000+'broken pipe: private-value');sys.stderr.flush();sys.exit(7)")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as client:
            with pytest.raises(ConnectionResetError):
                receive(client)
        wait_until(lambda: tunnel.snapshot()["failed_connections"] == 1)
        tunnel.check()
        snapshot = tunnel.snapshot()
        assert snapshot["failed_connections"] == 1
        assert snapshot["last_failure"]["reason"] == "broken_pipe"
        assert snapshot["last_failure"]["exit_code"] == 7
        assert "private-value" not in json.dumps(snapshot) + caplog.text
        assert len(json.dumps(snapshot)) < 1024
        assert "request was not replayed" in caplog.text
    finally:
        tunnel.close()


@pytest.mark.parametrize("ready", [
    "print('started',flush=True)",
    "print('start',end='',flush=True);time.sleep(0.05);print('ed',flush=True)",
], ids=["single-write", "fragmented-write"])
def test_session_close_reaps_children_and_threads_even_if_child_ignores_term(ready):
    kube = FakeKube(f"import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);{ready};time.sleep(60)")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as client:
            assert receive_exact(client, len(b"started\n")) == b"started\n"
            streams = list(tunnel._streams.values())
            started = time.monotonic()
            tunnel.close()
            assert time.monotonic() - started < 4
            assert all(stream.process.poll() is not None and not stream.thread.is_alive() for stream in streams)
            assert not tunnel._streams and not tunnel._accept_thread.is_alive()
            assert client.recv(1024) == b""
    finally:
        tunnel.close()


def test_connection_limit_bounds_children_without_disrupting_existing_stream():
    kube = FakeKube("import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(line);sys.stdout.buffer.flush()")
    tunnel = DockerTunnel(kube, IDENT, 0, max_connections=1).start()
    try:
        with connect(tunnel) as first:
            first.sendall(b"ready\n")
            assert receive_exact(first, len(b"ready\n")) == b"ready\n"
            with connect(tunnel) as second:
                with pytest.raises(ConnectionResetError):
                    receive(second)
            first.sendall(b"still-connected\n")
            assert receive_exact(first, len(b"still-connected\n")) == b"still-connected\n"
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
            assert receive_exact(client, len(b"before-revocation\n")) == b"before-revocation\n"
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


def test_transport_failure_preserves_existing_and_future_connections_without_replay():
    kube = FakeKube("import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(line);sys.stdout.buffer.flush()")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as existing:
            existing.sendall(b"ready\n")
            assert receive_exact(existing, len(b"ready\n")) == b"ready\n"
            kube.script = "import sys;sys.stderr.write('primary-failure');sys.exit(7)"
            with connect(tunnel) as failing:
                with pytest.raises(ConnectionResetError):
                    failing.recv(1024)
            existing.sendall(b"still-working\n")
            assert receive_exact(existing, len(b"still-working\n")) == b"still-working\n"
            wait_until(lambda: tunnel.snapshot()["failed_connections"] == 1)
            tunnel.check()
            assert tunnel.snapshot()["failed_connections"] == 1
            assert tunnel.snapshot()["last_failure"]["exit_code"] == 7
            kube.script = "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"
            with connect(tunnel) as later:
                later.sendall(b"new-request")
                later.shutdown(socket.SHUT_WR)
                assert receive(later) == b"new-request"
            existing.shutdown(socket.SHUT_WR)
            assert receive(existing) == b""
        wait_until(lambda: not tunnel._streams)
        assert tunnel._error is None and len(kube.commands) == 3
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


def test_partial_failed_request_is_never_replayed_and_cannot_succeed_cleanly():
    kube = FakeKube("import sys;sys.stdin.buffer.read(7);print('partial',flush=True);"
                    "sys.stdin.buffer.read(1);sys.exit(1)")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as client:
            client.sendall(b"request")
            assert receive_exact(client, 8) == b"partial\n"
            client.sendall(b"!")
            with pytest.raises(ConnectionResetError):
                receive(client)
        wait_until(lambda: not tunnel._streams)
        tunnel.check()
        assert len(kube.commands) == 1
        failure = tunnel.snapshot()["last_failure"]
        assert failure["received_bytes"] == 8 and failure["sent_bytes"] == 8
        assert failure["input_buffer_peak"] <= BUFFER_BYTES
        assert failure["output_buffer_peak"] <= BUFFER_BYTES
    finally:
        tunnel.close()


def test_child_launch_failure_is_local_and_does_not_expose_exception_text(monkeypatch, caplog):
    kube = FakeKube("import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    original = subprocess.Popen
    try:
        def unavailable(*_args, **_kwargs):
            raise OSError(24, "sensitive-command-value")
        monkeypatch.setattr(subprocess, "Popen", unavailable)
        with connect(tunnel) as failed:
            with pytest.raises(ConnectionResetError):
                receive(failed)
        wait_until(lambda: tunnel.snapshot()["failed_connections"] == 1)
        tunnel.check()
        assert tunnel.snapshot()["last_failure"]["errno"] == 24
        assert "sensitive-command-value" not in json.dumps(tunnel.snapshot()) + caplog.text
        monkeypatch.setattr(subprocess, "Popen", original)
        with connect(tunnel) as next_client:
            next_client.sendall(b"new request")
            next_client.shutdown(socket.SHUT_WR)
            assert receive(next_client) == b"new request"
    finally:
        tunnel.close()


def test_stream_diagnostics_are_bounded_copies_and_logging_is_rate_limited(caplog):
    kube = FakeKube("import sys;sys.stderr.write('secret-from-child');sys.exit(1)")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        for _ in range(4):
            with connect(tunnel) as client:
                with pytest.raises(ConnectionResetError):
                    receive(client)
        wait_until(lambda: tunnel.snapshot()["failed_connections"] == 4)
        snapshot = tunnel.snapshot()
        assert snapshot["failed_connections"] == 4
        assert len(json.dumps(snapshot)) < 1024
        snapshot["last_failure"]["reason"] = "corrupted"
        assert tunnel.snapshot()["last_failure"]["reason"] == "transport_exit"
        assert len([record for record in caplog.records if "connection failed" in record.message]) == 1
        assert "secret-from-child" not in caplog.text
        tunnel.check()
    finally:
        tunnel.close()


def test_uid_guard_rejection_still_revokes_other_connections_immediately():
    kube = GuardedKube("import sys\nfor line in sys.stdin.buffer:\n print(line.decode().strip(),flush=True)")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as existing:
            existing.sendall(b"before\n")
            assert receive_exact(existing, 7) == b"before\n"
            kube.remote_uid = "replacement-uid"
            with connect(tunnel) as changed:
                with pytest.raises(ConnectionResetError):
                    receive(changed)
            with pytest.raises(ConnectionResetError):
                existing.recv(1024)
        with pytest.raises(PodgroveError, match="UID changed"):
            tunnel.check()
        assert tunnel.snapshot()["failed_connections"] == 0
    finally:
        tunnel.close()


def test_guard_rejection_survives_fragmentation_and_bounded_stderr_suffix():
    from podgrove.docker_tunnel import _UID_REJECTED
    kube = GuardedKube(f"import sys,time;sys.stderr.write({_UID_REJECTED[:20]!r});sys.stderr.flush();"
                       f"time.sleep(.03);sys.stderr.write({_UID_REJECTED[20:]!r}+'x'*100000);sys.exit(126)")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as client:
            with pytest.raises(ConnectionResetError):
                receive(client)
        with pytest.raises(PodgroveError, match="UID changed"):
            tunnel.check()
        assert tunnel.snapshot()["failed_connections"] == 0
    finally:
        tunnel.close()


def test_slow_reader_large_binary_stream_keeps_buffers_bounded_and_other_stream_responsive():
    size = 8 * 1024 * 1024 + 17
    kube = FakeKube(f"import sys;sys.stdin.buffer.read(1);sys.stdout.buffer.write(b'x'*{size});"
                    "sys.stdout.buffer.flush();sys.stdin.buffer.read()")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as slow:
            slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            slow.sendall(b"x")
            wait_until(lambda: bool(tunnel._streams) and
                       next(iter(tunnel._streams.values())).output_buffer_peak == BUFFER_BYTES)
            stream = next(iter(tunnel._streams.values()))
            kube.script = "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"
            with connect(tunnel) as other:
                other.sendall(b"independent")
                other.shutdown(socket.SHUT_WR)
                assert receive(other) == b"independent"
            slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, BUFFER_BYTES)
            slow.shutdown(socket.SHUT_WR)
            received = 0
            while chunk := slow.recv(65536):
                assert chunk == b"x" * len(chunk)
                received += len(chunk)
            assert received == size
            assert stream.output_buffer_peak == BUFFER_BYTES
            assert stream.input_buffer_peak <= BUFFER_BYTES
            assert stream.sent_bytes == size
        tunnel.check()
    finally:
        tunnel.close()


class OutageKube(GuardedKube):
    def __init__(self):
        super().__init__("import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(line);sys.stdout.buffer.flush()")
        self.failure = None
        self.failures_seen = 0

    def call(self, *args, **kwargs):
        if self.failure:
            self.failures_seen += 1
            if self.failure == "exit":
                return subprocess.CompletedProcess(args, 1, "", "private-auth-response")
            if self.failure == "json":
                return subprocess.CompletedProcess(args, 0, "{incomplete", "")
            raise PodgroveError("private-error-response")
        return super().call(*args, **kwargs)


@pytest.fixture
def quick_retry(monkeypatch):
    from podgrove import docker_tunnel
    monkeypatch.setattr(docker_tunnel, "VERIFY_RETRY_INITIAL", .03)
    monkeypatch.setattr(docker_tunnel, "VERIFY_RETRY_MAX", .1)


@pytest.mark.parametrize("failure", ["exit", "json", "exception"])
def test_transient_verification_outage_gates_new_requests_preserves_peer_and_recovers(failure, quick_retry):
    kube = OutageKube()
    tunnel = DockerTunnel(kube, IDENT, 0, verification_interval=.05).start()
    try:
        with connect(tunnel) as existing:
            existing.sendall(b"before\n")
            assert receive_exact(existing, 7) == b"before\n"
            kube.failure = failure
            wait_until(lambda: tunnel.snapshot()["verification"]["state"] == "unavailable")
            # New operations are refused before opening a kubectl exec. An
            # established, previously proved stream survives the short outage.
            with connect(tunnel) as refused:
                with pytest.raises(ConnectionResetError):
                    refused.recv(1)
            assert len(kube.commands) == 1
            existing.sendall(b"during\n")
            assert receive_exact(existing, 7) == b"during\n"
            tunnel.check()
            snapshot = tunnel.snapshot()
            assert snapshot["verification"]["consecutive_failures"] >= 1
            assert snapshot["verification"]["retry_in_seconds"] is not None
            assert "private" not in json.dumps(snapshot)
            kube.failure = None
            wait_until(lambda: tunnel.snapshot()["verification"]["state"] == "verified")
            existing.sendall(b"after\n")
            assert receive_exact(existing, 6) == b"after\n"
            with connect(tunnel) as fresh:
                fresh.sendall(b"new\n")
                assert receive_exact(fresh, 4) == b"new\n"
            assert len(kube.commands) == 2
            assert tunnel.snapshot()["verification"]["consecutive_failures"] == 0
    finally:
        tunnel.close()
    assert not tunnel._verification_thread.is_alive()


def test_expired_ownership_proof_resets_only_streams_and_recovers_without_replay(quick_retry):
    kube = OutageKube()
    tunnel = DockerTunnel(kube, IDENT, 0, verification_interval=.03, max_verification_age=.2).start()
    try:
        with connect(tunnel) as client:
            client.sendall(b"started\n")
            assert receive_exact(client, 8) == b"started\n"
            kube.failure = "exit"
            with pytest.raises(ConnectionResetError):
                client.recv(1)
            wait_until(lambda: not tunnel._streams)
            assert tunnel.snapshot()["verification"]["state"] == "expired"
            assert tunnel.snapshot()["last_failure"]["reason"] == "ownership_verification_expired"
            assert len(kube.commands) == 1
            tunnel.check()
            kube.failure = None
            wait_until(lambda: tunnel.snapshot()["verification"]["state"] == "verified")
            with connect(tunnel) as fresh:
                fresh.sendall(b"recovered\n")
                assert receive_exact(fresh, 10) == b"recovered\n"
            assert len(kube.commands) == 2
    finally:
        tunnel.close()


def test_recovery_proof_with_changed_uid_is_fatal_even_after_transient_outage(quick_retry):
    kube = OutageKube()
    tunnel = DockerTunnel(kube, IDENT, 0, verification_interval=.03).start()
    try:
        with connect(tunnel) as client:
            client.sendall(b"open\n")
            assert receive_exact(client, 5) == b"open\n"
            kube.failure = "exit"
            wait_until(lambda: tunnel._verification_unavailable)
            kube.pod["metadata"]["uid"] = "replacement-uid"
            kube.failure = None
            wait_until(lambda: tunnel._error is not None)
            with pytest.raises(ConnectionResetError):
                client.recv(1)
            with pytest.raises(PodgroveError, match="replaced"):
                tunnel.check()
            assert len(kube.commands) == 1
    finally:
        tunnel.close()


def test_legacy_connection_read_outage_recovers_without_waiting_full_normal_interval(quick_retry):
    kube = OutageKube()
    kube.pod["spec"].pop("containers")
    kube.command = lambda *args: FakeKube.command(kube, *args)
    tunnel = DockerTunnel(kube, IDENT, 0, verification_interval=30).start()
    try:
        kube.failure = "exit"
        with connect(tunnel) as refused:
            with pytest.raises(ConnectionResetError):
                refused.recv(1)
        wait_until(lambda: tunnel._verification_unavailable)
        kube.failure = None
        wait_until(lambda: not tunnel._verification_unavailable, timeout=2)
        with connect(tunnel) as client:
            client.sendall(b"recovered\n")
            assert receive_exact(client, 10) == b"recovered\n"
        tunnel.check()
        assert len(kube.commands) == 1
    finally:
        tunnel.close()


def test_slow_pending_read_cannot_extend_grace_and_close_cancels_actual_process(monkeypatch):
    from types import SimpleNamespace

    from podgrove import docker_tunnel
    from podgrove.process import run

    # Control the tunnel clock so ownership age cannot race the assertions.
    # Sockets, the sleeping subprocess, and cancellation timing stay real.
    now = [1000.0]
    monkeypatch.setattr(docker_tunnel, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    kube = OutageKube()
    started = threading.Event()
    original = kube.call
    original_popen = subprocess.Popen
    children = []
    stalled_reads = []
    stalled = False

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        children.append(process)
        if kwargs.get("start_new_session"):
            stalled_reads.append(process)
            started.set()
        return process

    def call(*args, **kwargs):
        if stalled:
            return run([sys.executable, "-c", "import time;time.sleep(60)"], **kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(kube, "call", call)
    tunnel = DockerTunnel(kube, IDENT, 0, verification_interval=30, max_verification_age=120).start()
    try:
        with connect(tunnel) as client:
            client.sendall(b"open\n")
            assert receive_exact(client, 5) == b"open\n"
            stalled = True
            now[0] = 1030.0
            assert started.wait(2)
            assert len(stalled_reads) == 1 and stalled_reads[0].poll() is None
            assert tunnel.snapshot()["verification"]["state"] == "verifying"
            now[0] = 1119.0
            client.sendall(b"before-expiry\n")
            assert receive_exact(client, 14) == b"before-expiry\n"
            assert tunnel.snapshot()["verification"]["state"] == "verifying"
            now[0] = 1121.0
            with pytest.raises(ConnectionResetError):
                client.recv(1)
            assert tunnel.snapshot()["verification"]["state"] == "expired"
            assert tunnel.snapshot()["last_failure"]["reason"] == "ownership_verification_expired"
            assert stalled_reads[0].poll() is None
            assert len(kube.commands) == 1
        before = time.monotonic()
        tunnel.close()
        assert time.monotonic() - before < 2
    finally:
        tunnel.close()
    assert all(process.poll() is not None for process in children)
    assert not tunnel._verification_thread.is_alive()
    assert not any(thread.name == "podgrove-command-cancel" for thread in threading.enumerate())


def test_initial_unavailable_proof_never_opens_listener():
    kube = OutageKube()
    kube.failure = "exit"
    tunnel = DockerTunnel(kube, IDENT, 0)
    with pytest.raises(PodgroveError, match="verification is unavailable"):
        tunnel.start()
    assert tunnel._listener is None and not kube.commands


def test_verification_retry_delay_is_bounded_and_snapshot_does_not_expose_reason_payload():
    from podgrove.docker_tunnel import _VerificationUnavailable
    tunnel = DockerTunnel(OutageKube(), IDENT, 0)
    delays = [tunnel._verification_failed(_VerificationUnavailable("api_read_failed")) for _ in range(50)]
    assert delays[:7] == [1, 2, 4, 8, 16, 30, 30]
    assert delays[-1] == 30
    snapshot = tunnel.snapshot()
    snapshot["verification"]["reason"] = "modified-copy"
    assert tunnel.snapshot()["verification"]["reason"] == "api_read_failed"


def test_stream_transport_forces_websocket_despite_inherited_spdy_override(monkeypatch):
    monkeypatch.setenv("KUBECTL_REMOTE_COMMAND_WEBSOCKETS", "false")
    kube = FakeKube("import os,sys;sys.stdout.write(os.environ['KUBECTL_REMOTE_COMMAND_WEBSOCKETS'])")
    tunnel = DockerTunnel(kube, IDENT, 0).start()
    try:
        with connect(tunnel) as client:
            assert receive(client) == b"true"
        tunnel.check()
        assert len(kube.commands) == 1
    finally:
        tunnel.close()
