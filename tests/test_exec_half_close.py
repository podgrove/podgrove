"""Reproduce the Docker/client port-forward EOF failure without a real Engine.

The fake HTTP Engine models containerd's one-second port-forward shutdown after
stdin EOF. Docker's exec inspection currently accepts ExitCode=0 even while
Running=true. These probes use only loopback sockets and installed client
binaries; they never contact a Docker daemon or Kubernetes cluster.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time

import pytest


class FakeEngine(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def respond(self, body, code=200):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("API-Version", "1.53")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        labels = {"com.docker.compose.project": "transportprobe", "com.docker.compose.service": "probe",
                  "com.docker.compose.container-number": "1", "com.docker.compose.oneoff": "False"}
        if path.endswith("/containers/json"):
            self.respond([{"Id": "fixture", "Names": ["/transportprobe-probe-1"], "Image": "alpine:3.21",
                           "State": "running", "Status": "Up", "Labels": labels}])
        elif path.endswith("/containers/fixture/json"):
            self.respond({"Id": "fixture", "Name": "/transportprobe-probe-1",
                          "Config": {"Tty": False, "Labels": labels}, "State": {"Running": True}})
        elif path.endswith("/exec/probe/json"):
            running = time.monotonic() < self.server.finished
            self.server.inspect_running = running
            self.respond({"ID": "probe", "Running": running, "ExitCode": 0 if running else 7})
        else:
            self.respond({"message": f"Unexpected test request: {path}"}, 404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path.endswith("/containers/fixture/exec"):
            self.respond({"Id": "probe"}, 201)
        elif self.path.endswith("/exec/probe/start"):
            self.send_response(101, "UPGRADED")
            self.send_header("Content-Type", "application/vnd.docker.raw-stream")
            self.send_header("Connection", "Upgrade")
            self.send_header("Upgrade", "tcp")
            self.end_headers()
            self.wfile.flush()
            self.server.finished = time.monotonic() + 2
            eof = None
            self.connection.settimeout(0.025)
            while time.monotonic() < self.server.finished:
                if eof is None:
                    try:
                        if not self.connection.recv(4096):
                            eof = time.monotonic()
                    except socket.timeout:
                        pass
                else:
                    time.sleep(0.025)
                if eof and self.server.truncate and time.monotonic() - eof >= 1:
                    self.close_connection = True
                    return
            for channel, output in ((1, b"delayed stdout\n"), (2, b"delayed stderr\n")):
                self.wfile.write(bytes((channel, 0, 0, 0)) + struct.pack(">I", len(output)) + output)
            self.wfile.flush()
            self.close_connection = True
        else:
            self.respond({"message": f"Unexpected test request: {self.path}"}, 404)


@contextmanager
def fake_engine(*, truncate, handler=FakeEngine):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    server.truncate = truncate
    server.finished = 0
    server.inspect_running = None
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.fixture
def docker_client():
    path = shutil.which("docker")
    if path is None:
        pytest.skip("Docker client binary is not installed; no daemon is required")
    return path


@pytest.mark.parametrize("client,truncate,hold_stdin,interactive", [
    ("docker", False, False, True),
    ("docker", True, False, True),
    ("compose", True, False, True),
    ("compose", True, False, False),
    ("compose", True, True, True),
])
def test_client_eof_reproduction(docker_client, tmp_path, client, truncate, hold_stdin, interactive):
    with fake_engine(truncate=truncate) as server:
        # Preserve the explicitly isolated CLI config: Compose plugins may live
        # under its cli-plugins directory, particularly on hosted macOS runners.
        env = {key: value for key, value in os.environ.items()
               if (not key.startswith("DOCKER_") or key == "DOCKER_CONFIG") and key != "BUILDX_BUILDER"}
        env.update(DOCKER_HOST=f"tcp://127.0.0.1:{server.server_port}", DOCKER_API_VERSION="1.53")
        if client == "compose":
            version = subprocess.run([docker_client, "compose", "version"], env=env,
                                     capture_output=True, timeout=10)
            if version.returncode:
                pytest.skip("Compose client plugin is not installed")
            compose: Path = tmp_path / "compose.yaml"
            compose.write_text("name: transportprobe\nservices:\n  probe:\n    image: alpine:3.21\n")
            command = [docker_client, "compose", "-f", str(compose), "exec", "-T"]
            if not interactive:
                command.append("--interactive=false")
            command.append("probe")
        else:
            command = [docker_client, "exec", "-i", "fixture"]
        command += ["sh", "-c", "sleep 2; printf 'delayed stdout\\n'; printf 'delayed stderr\\n' >&2; exit 7"]
        if hold_stdin:
            # communicate() would close stdin early and reproduce the failure.
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
                try:
                    process.wait(timeout=15)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    process.stdin.close()
                stdout.seek(0)
                stderr.seek(0)
                observed = (process.returncode, stdout.read().decode(), stderr.read().decode())
        else:
            result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=15)
            observed = (result.returncode, result.stdout, result.stderr)
        if truncate and not hold_stdin:
            assert observed == (0, "", "")
            assert server.inspect_running is True
        else:
            assert observed == (7, "delayed stdout\n", "delayed stderr\n")
            assert server.inspect_running is False


class LargeOutputEngine(FakeEngine):
    """The real clients and dial-stdio bridge must drain output after input EOF."""

    def do_POST(self):
        if not self.path.endswith("/exec/probe/start"):
            return super().do_POST()
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(101, "UPGRADED")
        self.send_header("Content-Type", "application/vnd.docker.raw-stream")
        self.send_header("Connection", "Upgrade")
        self.send_header("Upgrade", "tcp")
        self.end_headers()
        self.wfile.flush()
        self.server.finished = float("inf")
        self.connection.settimeout(5)
        assert self.connection.recv(1) == b""  # Noninteractive client closed only its input.
        self.server.stdin_closed = True
        for start in range(0, len(self.server.payload), 32768):
            chunk = self.server.payload[start:start + 32768]
            self.wfile.write(bytes((1, 0, 0, 0)) + struct.pack(">I", len(chunk)) + chunk)
            self.wfile.flush()
        error = b"expected stderr\n"
        self.wfile.write(bytes((2, 0, 0, 0)) + struct.pack(">I", len(error)) + error)
        self.wfile.flush()
        self.server.finished = time.monotonic()
        self.close_connection = True


@pytest.mark.parametrize("client", ["docker", "compose"])
@pytest.mark.parametrize("size", [29284, 8 * 1024 * 1024 + 17], ids=["export-sized", "multi-megabyte"])
def test_real_clients_and_dial_stdio_stream_binary_output_through_relay(docker_client, tmp_path, client, size):
    from podgrove.docker_tunnel import DockerTunnel
    from test_docker_tunnel import FakeKube, IDENT

    # No real Engine or Kubernetes is involved. Unlike raw-pipe relay tests,
    # this retains Docker's HTTP upgrade, multiplexed frames, exec inspection,
    # stdin EOF and real dial-stdio child shutdown semantics end to end.
    with fake_engine(truncate=False, handler=LargeOutputEngine) as server:
        server.payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
        server.stdin_closed = False
        kube = FakeKube("")
        def command(*args):
            kube.commands.append(args)
            return [docker_client, "--host", f"tcp://127.0.0.1:{server.server_port}", "system", "dial-stdio"]
        kube.command = command
        tunnel = DockerTunnel(kube, IDENT, 0).start()
        try:
            env = {key: value for key, value in os.environ.items()
                   if (not key.startswith("DOCKER_") or key == "DOCKER_CONFIG") and key != "BUILDX_BUILDER"}
            env.update(DOCKER_HOST=f"tcp://127.0.0.1:{tunnel.port}", DOCKER_API_VERSION="1.53")
            if client == "compose":
                version = subprocess.run([docker_client, "compose", "version"], env=env,
                                         capture_output=True, timeout=10)
                if version.returncode:
                    pytest.skip("Compose client plugin is not installed")
                compose = tmp_path / "compose.yaml"
                compose.write_text("name: transportprobe\nservices:\n  probe:\n    image: alpine:3.21\n")
                args = [docker_client, "compose", "-f", str(compose), "exec", "-T", "probe"]
            else:
                args = [docker_client, "exec", "-i", "fixture"]
            result = subprocess.run([*args, "sh", "-c", "fixture-only"], env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=30)
            assert result.returncode == 7
            assert result.stdout == server.payload
            assert result.stderr == b"expected stderr\n"
            assert server.stdin_closed and server.inspect_running is False
            tunnel.check()
            assert tunnel.snapshot()["failed_connections"] == 0
        finally:
            tunnel.close()
        assert not tunnel._streams
        assert not tunnel._accept_thread.is_alive()
        assert not tunnel._verification_thread.is_alive()
