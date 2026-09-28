"""Startup phases reach the real session log before blocked work completes."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from podgrove import runtime, state


@pytest.mark.parametrize("phase,message", [
    ("configuration", "loading and validating Compose configuration"),
    ("engine", "waiting for engine Pod readiness"),
    ("docker_api", "opening the Docker API connection"),
    ("docker_ready", "checking Docker engine readiness"),
    ("initial_sync", "copying the initial workspace snapshot"),
    ("existing_services", "checking existing Compose services"),
    ("compose_up", "building and starting Compose services"),
    ("service_readiness", "waiting for Compose service readiness"),
    ("port_forwards", "opening application port forwards"),
])
def test_blocked_startup_phase_is_flushed_to_session_log_without_configuration(
        tmp_path, monkeypatch, phase, message):
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "worktree"
    root.mkdir()
    path = state.state_path(root, "test-context")
    socket_path = Path("/tmp") / f"pg-progress-{os.getpid()}-{time.time_ns()}.sock"
    data = {"root": str(root), "identity": state.identity(root), "context": "test-context",
            "namespace": "test-namespace", "token": "private-test-token", "socket": str(socket_path),
            "status": "starting", "timeout": 60, "ttl_seconds": 3600, "mr_url": ""}
    state.write(path, data)
    program = '''
import sys, time
from pathlib import Path
from types import SimpleNamespace
from podgrove import runtime
root, phase = Path(sys.argv[2]), sys.argv[3]
def block(selected):
    if selected == phase:
        (root / "blocked").write_text(selected)
        time.sleep(60)
        raise AssertionError("The pending operation must be cancelled, not completed")
noop = lambda *args, **kwargs: None
config = SimpleNamespace(root=root, forward=[], ttl_seconds=3600)
def load(*args):
    block("configuration")
    return config
runtime.load_config = load
model = {"services": {"app": {"environment": {"TOKEN": "unlogged-test-value"}}}}
runtime.Compose = lambda *args: SimpleNamespace(config=config, model=lambda: model, validate=noop,
    sync_paths=lambda *_: [], command=lambda *args: ["docker", "compose", *args],
    published_ports=lambda *_: [], has_watch=lambda *_: False)
runtime.Kube = lambda *args, **kwargs: SimpleNamespace(wait=lambda *_: block("engine"), heartbeat=noop)
api = SimpleNamespace(check=noop, close=noop, snapshot=lambda: {"verification": {"state": "verified"}})
def api_start():
    block("docker_api")
    return api
runtime.DockerTunnel = lambda *args: SimpleNamespace(start=api_start)
compose_started = False
def run(command, **kwargs):
    global compose_started
    block("docker_ready" if command == ["docker", "info"] else "compose_up")
    if command != ["docker", "info"]:
        compose_started = True
    return SimpleNamespace(stdout="", stderr="")
runtime.run = run
sync = SimpleNamespace(start=lambda: block("initial_sync"), close=noop, cancel=noop, sync_once=lambda: 0)
runtime.Synchronizer = lambda *args, **kwargs: sync
def services(*args, **kwargs):
    block("service_readiness" if compose_started else "existing_services")
    return [{"Service": "app", "State": "running"}]
runtime.service_status = services
runtime.port_plan = lambda *args, **kwargs: [{"service": "app", "target": 8000,
                                              "published": 8000, "local": 12345}]
def forward_start():
    block("port_forwards")
    raise AssertionError("This test must stop before opening a real forward")
runtime.Tunnel = lambda *args: SimpleNamespace(start=forward_start, close=noop,
                                               snapshot=lambda: {"state": "connecting"})
raise SystemExit(runtime.serve(Path(sys.argv[1])))
'''
    log_path = path.with_suffix(".log")
    # Neither -u nor PYTHONUNBUFFERED: production must flush its buffered log.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    with log_path.open("wb") as log:
        process = subprocess.Popen([sys.executable, "-c", program, str(path), str(root), phase],
                                   stdout=log, stderr=log, env=env)
    try:
        deadline = time.monotonic() + 10
        while not (root / "blocked").exists():
            assert process.poll() is None, log_path.read_text()
            assert time.monotonic() < deadline, log_path.read_text()
            time.sleep(0.01)
        output = log_path.read_text()
        assert output.splitlines()[-1] == f"Startup: {message}"
        assert len(output.encode()) < 2048
        assert "unlogged-test-value" not in output
        assert data["token"] not in output
        assert str(root) not in output
        assert "Startup: ready" not in output
        assert process.poll() is None
        assert runtime.control(data, "ping")["status"] == "starting"
        assert runtime.control(data, "stop")["ok"] is True
        assert process.wait(timeout=10) == 0, log_path.read_text()
        assert state.read(path)["status"] == "disconnected"
        assert not socket_path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        socket_path.unlink(missing_ok=True)
