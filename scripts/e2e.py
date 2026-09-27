#!/usr/bin/env python3
"""Run real remote-engine acceptance tests in owned local Docker-in-Docker containers.

This harness never invokes kubectl and never uses a Kubernetes cluster. It requires
a local Unix-socket Docker engine with privileged-container support.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Harness:
    def __init__(self, output: Path, mongo: bool):
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.mongo = mongo
        self.run_id = f"podgrove-e2e-{uuid.uuid4().hex[:10]}"
        self.engines = []
        self.syncers = []
        self.processes = []
        self.results = []
        self.env = dict(os.environ)
        self.started = time.monotonic()

    def run(self, args, env=None, timeout=180, check=True):
        result = subprocess.run(args, env=env or self.env, text=True, capture_output=True, timeout=timeout)
        with (self.output / "commands.log").open("a") as handle:
            handle.write(f"$ {' '.join(map(str, args))}\n{result.stdout}{result.stderr}\nexit={result.returncode}\n")
        if check and result.returncode:
            raise RuntimeError(f"Command failed ({result.returncode}): {args}\n{result.stderr[-4000:]}")
        return result

    def record(self, name, **evidence):
        self.results.append({"test": name, "passed": True, **evidence})
        self.save("running")
        print(f"PASS {name}: {json.dumps(evidence, sort_keys=True)}", flush=True)

    def save(self, status, error=None):
        data = {
            "status": status,
            "scope": "Local Docker-in-Docker remote engines; Kubernetes scheduling/port-forward NOT verified",
            "run_id": self.run_id,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "tests": self.results,
        }
        if error:
            data["error"] = error
        (self.output / "results.json").write_text(json.dumps(data, indent=2) + "\n")

    def require_local_engine(self):
        required = (4 if self.mongo else 2) * 1024 ** 3
        available = min(shutil.disk_usage(ROOT).free, shutil.disk_usage(tempfile.gettempdir()).free)
        if available < required:
            raise RuntimeError(
                f"Local Docker E2E needs {required // 1024 ** 3} GiB of host disk headroom; "
                f"only {available / 1024 ** 3:.2f} GiB is free. No test engine was created."
            )
        endpoint = self.env.get("DOCKER_HOST")
        if not endpoint:
            result = self.run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
            endpoint = result.stdout.strip()
        if not endpoint.startswith("unix://"):
            raise RuntimeError(f"This harness only operates on a local Unix-socket Docker engine: {endpoint}")
        result = self.run(["docker", "info", "--format", "{{.Name}} {{.OSType}} {{.MemTotal}}"])
        self.record("local_engine_guard", endpoint=endpoint, engine=result.stdout.strip())

    def engine(self, suffix):
        from podgrove.kube import IMAGE

        name = f"{self.run_id}-{suffix}"
        volume = f"{name}-docker"
        self.run(["docker", "volume", "create", "--label", f"io.podgrove.test-run={self.run_id}", volume])
        self.engines.append((name, volume))
        self.run([
            "docker", "run", "-d", "--name", name, "--label", f"io.podgrove.test-run={self.run_id}",
            "--privileged", "--cpus", "2", "--memory", "1536m", "--pids-limit", "1024", "--stop-timeout", "60",
            "-e", "DOCKER_TLS_CERTDIR=", "-p", "127.0.0.1::2375", "-p", "127.0.0.1::8080",
            "-p", "127.0.0.1::8081", "-v", f"{volume}:/var/lib/docker",
            IMAGE, "--host=tcp://0.0.0.0:2375", "--tls=false",
        ], timeout=300)
        env = dict(self.env)
        for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_TLS", "COMPOSE_PROJECT_NAME"):
            env.pop(key, None)
        env, app_port, watch_port = self.endpoints(name, env)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.run(["docker", "info"], env, timeout=5, check=False).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f"Docker-in-Docker engine {name} did not become ready")
        for image in ("python:3.12-alpine", "redis:7-alpine", *(["mongo:7"] if self.mongo else [])):
            self.run(["docker", "pull", image], env, timeout=300)
        return env, app_port, watch_port

    def endpoints(self, name, env):
        """Docker may allocate new dynamic host ports after an outer-container restart."""
        ports = [
            int(self.run(["docker", "port", name, f"{port}/tcp"]).stdout.strip().rsplit(":", 1)[1])
            for port in (2375, 8080, 8081)
        ]
        updated = dict(env)
        updated["DOCKER_HOST"] = f"tcp://127.0.0.1:{ports[0]}"
        return updated, ports[1], ports[2]

    def compose(self, root, env, files=None, profiles=None):
        from podgrove.config import load_config
        from podgrove.compose import Compose
        from podgrove.sync import Synchronizer

        config = load_config(root, files=files)
        if profiles:
            config.profiles.extend(profiles)
        compose = Compose(config)
        # Compose normalization is a local parse: no remote objects exist yet.
        model = compose.model()
        compose.validate(model)
        syncer = Synchronizer(root, compose.sync_paths(model), env, hashlib.sha256(str(root).encode()).hexdigest()[:12])
        self.syncers.append(syncer)
        syncer.start()
        return compose, syncer, model

    def diagnostics(self):
        for name, _volume in self.engines:
            result = self.run(["docker", "logs", name], check=False)
            (self.output / f"{name}.log").write_text(result.stdout + result.stderr)
            result = self.run(["docker", "exec", name, "docker", "ps", "-a"], check=False)
            (self.output / f"{name}-containers.log").write_text(result.stdout + result.stderr)

    def cleanup(self):
        for process, stream in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            stream.close()
        for syncer in reversed(self.syncers):
            try:
                syncer.close()
            except Exception as exc:
                print(f"sync helper cleanup: {exc}", file=sys.stderr)
        for name, volume in reversed(self.engines):
            self.run(["docker", "rm", "-f", name], check=False)
            self.run(["docker", "volume", "rm", volume], check=False)
        remaining = self.run(["docker", "ps", "-aq", "--filter", f"label=io.podgrove.test-run={self.run_id}"])
        volumes = self.run(["docker", "volume", "ls", "-q", "--filter", f"label=io.podgrove.test-run={self.run_id}"])
        if remaining.stdout.strip() or volumes.stdout.strip():
            raise RuntimeError("Owned local Docker test resources remain after cleanup")
        self.record("owned_resources_cleanup", containers=0, volumes=0)


def http(port, path="/", method="GET"):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def wait_value(port, key, expected, timeout=5):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            if http(port).get(key) == expected:
                return round(time.monotonic() - start, 3)
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"HTTP {key} did not become {expected!r} within {timeout}s")


def websocket_echo(port):
    key = base64.b64encode(os.urandom(16)).decode()
    payload = b"podgrove-websocket-echo"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall((
            f"GET /ws HTTP/1.1\r\nHost: localhost:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        reader = connection.makefile("rb")
        status = reader.readline()
        assert b"101" in status, status
        headers = {}
        while (line := reader.readline()) != b"\r\n":
            if not line:
                raise AssertionError("WebSocket handshake closed early")
            field, value = line.decode().split(":", 1)
            headers[field.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        assert headers["sec-websocket-accept"] == expected
        mask = os.urandom(4)
        connection.sendall(bytes([129, 128 | len(payload)]) + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(payload)))
        head = reader.read(2)
        length = head[1] & 127
        if length == 126:
            length = struct.unpack("!H", reader.read(2))[0]
        assert reader.read(length) == payload


def execute(harness):
    from podgrove.compose import Compose
    from podgrove.config import load_config
    from podgrove.errors import PodgroveError

    harness.require_local_engine()
    with tempfile.TemporaryDirectory(prefix="podgrove-e2e-") as scratch:
        scratch = Path(scratch)
        first = scratch / "worktree-a"
        second = scratch / "worktree-b"
        watch = scratch / "watch"
        for target in (first, second):
            shutil.copytree(ROOT / "tests/fixtures/parity", target)
        shutil.copytree(ROOT / "tests/fixtures/watch", watch)
        unknown = scratch / "unknown"
        unknown.mkdir()
        shutil.copy(ROOT / "tests/fixtures/invalid/podgrove.yml", unknown)
        try:
            load_config(unknown)
        except PodgroveError as exc:
            assert "unknown_deliberate_fixture_key" in str(exc)
            harness.record("unknown_config_refused_before_engine_creation", error=str(exc), engines=len(harness.engines))
        else:
            raise AssertionError("Unknown configuration key was accepted")
        unsupported = scratch / "unsupported"
        unsupported.mkdir()
        shutil.copy(ROOT / "tests/fixtures/invalid/compose.yml", unsupported)
        try:
            Compose(load_config(unsupported, files=["compose.yml"])).model()
        except PodgroveError as exc:
            assert "network_mode" in str(exc)
            harness.record("unsupported_key_refused_before_engine_creation", error=str(exc), engines=len(harness.engines))
        else:
            raise AssertionError("Unsupported Compose key was accepted")
        override = Compose(load_config(first, files=["compose.yml", "compose.override.yml"])).model()
        assert [str(port["published"]) for port in override["services"]["api"]["ports"]] == ["8082"]
        harness.record("compose_override_merge", published_port="8082")
        env_a, port_a, watch_port = harness.engine("a")
        env_b, port_b, _ = harness.engine("b")
        assert port_a != port_b
        stacks = []
        for worktree, env in ((first, env_a), (second, env_b)):
            compose, syncer, model = harness.compose(worktree, env, profiles=["database"] if harness.mongo else None)
            start = time.monotonic()
            harness.run(compose.command("up", "-d", "--wait", "--wait-timeout", "120"), env, timeout=240)
            stacks.append((compose, syncer, model))
            harness.record(f"stack_{worktree.name}", up_seconds=round(time.monotonic() - start, 3), services=len(model["services"]))
        compose_a, sync_a, model_a = stacks[0]
        compose_b, _sync_b, _model_b = stacks[1]
        state = http(port_a)
        expected_files = sorted(str(p.relative_to(first / "content")) for p in (first / "content").rglob("*") if p.is_file())
        assert state["files"] == expected_files
        assert state["single"] == "single-file-original\n"
        assert state["config"] == "local-file-config\n"
        assert state["secret"] == "public-fixture-not-a-real-secret\n"
        assert state["overlay"] == "overlay-mounted\n"
        assert state["mode"] == "overlay" and state["env_file"] == "from-env-file"
        assert state["initialized"] == "ready"
        harness.record("initial_bind_config_secret_overlay_parity", observed=state)
        dns = harness.run(compose_a.command("exec", "-T", "api", "python", "-c", "import socket; print(socket.gethostbyname('redis')); print(socket.gethostbyname('api-alias'))"), env_a)
        assert len(dns.stdout.strip().splitlines()) == 2
        harness.record("service_and_alias_dns", resolved=dns.stdout.strip().splitlines())
        actual_networks = harness.run(["docker", "network", "ls", "--format", "{{.Name}}"], env_a).stdout.splitlines()
        actual_volumes = harness.run(["docker", "volume", "ls", "--format", "{{.Name}}"], env_a).stdout.splitlines()
        assert model_a["networks"]["grove"]["name"] in actual_networks
        for key in ("state", "redis-data"):
            assert model_a["volumes"][key]["name"] in actual_volumes
        harness.record("compose_engine_names", networks=actual_networks, named_volumes=[v for v in actual_volumes if "parity" in v])
        websocket_echo(port_a)
        harness.record("websocket_echo", port=port_a)
        image_before_edit = harness.run(compose_a.command("images", "-q", "api"), env_a).stdout.strip()
        (first / "content/message.txt").write_text("edited-remotely\n")
        (first / "content/live.py").write_text('VALUE = "python-edited"\n')
        replacement = first / "single.replacement"
        replacement.write_text("single-file-edited\n")
        replacement.replace(first / "single.txt")
        (first / "content/nested/value.txt").unlink()
        (first / "content/new.txt").write_text("new-file\n")
        started = time.monotonic()
        changed = sync_a.sync_once()
        wait_value(port_a, "message", "edited-remotely\n")
        elapsed = time.monotonic() - started
        assert elapsed < 5
        assert http(port_a)["single"] == "single-file-edited\n"
        assert http(port_a)["python_value"] == "python-edited"
        assert http(port_a)["files"] == ["live.py", "message.txt", "new.txt"]
        assert http(port_b)["message"] == "initial-message\n"
        image_after_edit = harness.run(compose_a.command("images", "-q", "api"), env_a).stdout.strip()
        assert image_before_edit == image_after_edit
        harness.record("incremental_sync_file_bind_create_delete_isolation", changes=changed, elapsed_seconds=round(elapsed, 3))
        harness.record("python_edit_served_without_rebuild", elapsed_seconds=round(elapsed, 3), image_id=image_after_edit)
        assert http(port_a, "/counter", "POST")["count"] == 1
        assert http(port_b, "/counter")["count"] == 0
        harness.run(compose_a.command("exec", "-T", "redis", "redis-cli", "SET", "podgrove-isolation", "engine-a"), env_a)
        other = harness.run(compose_b.command("exec", "-T", "redis", "redis-cli", "--raw", "GET", "podgrove-isolation"), env_b)
        assert other.stdout.strip() == ""
        harness.run(compose_a.command("down"), env_a)
        started = time.monotonic()
        harness.run(compose_a.command("up", "-d", "--wait", "--wait-timeout", "120"), env_a)
        assert http(port_a, "/counter")["count"] == 1
        persisted = harness.run(compose_a.command("exec", "-T", "redis", "redis-cli", "--raw", "GET", "podgrove-isolation"), env_a)
        assert persisted.stdout.strip() == "engine-a"
        harness.record("named_volume_restart_and_redis_isolation", cached_up_seconds=round(time.monotonic() - started, 3))
        if harness.mongo:
            harness.run(compose_a.command("exec", "-T", "mongodb", "mongosh", "--quiet", "--eval", "db.getSiblingDB('podgrove').probe.insertOne({value:'a'})"), env_a)
            other = harness.run(compose_b.command("exec", "-T", "mongodb", "mongosh", "--quiet", "--eval", "db.getSiblingDB('podgrove').probe.countDocuments({})"), env_b)
            assert other.stdout.strip() == "0"
            harness.record("mongo_engine_isolation", other_engine_documents=0)
        harness.run(["docker", "restart", "--timeout", "60", harness.engines[0][0]])
        old_endpoint = env_a["DOCKER_HOST"]
        env_a, port_a, watch_port = harness.endpoints(harness.engines[0][0], env_a)
        for syncer in harness.syncers:
            if syncer.env.get("DOCKER_HOST") == old_endpoint:
                syncer.env["DOCKER_HOST"] = env_a["DOCKER_HOST"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if harness.run(["docker", "info"], env_a, timeout=5, check=False).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Restarted Docker engine did not become ready")
        harness.run(compose_a.command("up", "-d", "--wait", "--wait-timeout", "120"), env_a)
        assert http(port_a, "/counter")["count"] == 1
        assert http(port_a)["python_value"] == "python-edited"
        harness.record("docker_daemon_restart_preserves_named_data_and_binds", count=1)
        watch_compose, _watch_sync, watch_model = harness.compose(watch, env_a, files=["compose.yml"])
        assert watch_compose.has_watch(watch_model)
        started = time.monotonic()
        harness.run(watch_compose.command("up", "-d", "--build", "--wait", "--wait-timeout", "120"), env_a, timeout=300)
        harness.record("build_context_upload", first_build_up_seconds=round(time.monotonic() - started, 3))
        started = time.monotonic()
        harness.run(watch_compose.command("up", "-d", "--build", "--wait", "--wait-timeout", "120"), env_a, timeout=300)
        harness.record("cached_build_context_upload", cached_build_up_seconds=round(time.monotonic() - started, 3))
        initial_image = harness.run(watch_compose.command("images", "-q", "api"), env_a).stdout.strip()
        stream = (harness.output / "compose-watch.log").open("w")
        process = subprocess.Popen(watch_compose.command("watch", "--no-up"), env=env_a, stdout=stream, stderr=subprocess.STDOUT)
        harness.processes.append((process, stream))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            text = (harness.output / "compose-watch.log").read_text()
            if "Watch enabled" in text or "watching" in text.lower():
                break
            if process.poll() is not None:
                raise RuntimeError(f"Compose watch exited: {text}")
            time.sleep(0.1)
        else:
            raise RuntimeError("Compose watch did not report readiness")
        started = time.monotonic()
        (watch / "content/message.txt").write_text("watch-synchronized\n")
        wait_value(watch_port, "message", "watch-synchronized\n")
        elapsed = time.monotonic() - started
        final_image = harness.run(watch_compose.command("images", "-q", "api"), env_a).stdout.strip()
        assert initial_image == final_image
        harness.record("remote_compose_watch_without_rebuild", elapsed_seconds=round(elapsed, 3), image_id=final_image)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/docker-e2e")
    parser.add_argument("--mongo", action="store_true", help="Also run MongoDB isolation on both engines")
    args = parser.parse_args()
    harness = Harness(args.output, args.mongo)
    error = None
    try:
        execute(harness)
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(error, file=sys.stderr)
        harness.diagnostics()
    finally:
        try:
            harness.cleanup()
        except Exception as exc:
            error = f"{error or ''}\nCleanup failed: {exc}"
        harness.save("failed" if error else "passed", error)
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
