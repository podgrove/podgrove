"""Real process/signal checks: authenticated stop interrupts blocked session work."""
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from podgrove import runtime, state
from podgrove.errors import PodgroveError


@pytest.mark.parametrize("blocked_phase", ["startup", "heartbeat", "health"])
def test_authenticated_stop_cancels_blocked_work_and_removes_socket(tmp_path, monkeypatch, blocked_phase):
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "worktree"
    root.mkdir()
    path = state.state_path(root, "test-context")
    # macOS AF_UNIX path length is 104 bytes, independent of pytest's temp root.
    socket_path = Path("/tmp") / f"pg-cancel-{os.getpid()}-{time.time_ns()}.sock"
    data = {"root": str(root), "identity": state.identity(root), "context": "test-context",
            "namespace": "podgrove-testing", "token": "private-test-token", "socket": str(socket_path),
            "status": "starting", "timeout": 60, "ttl_seconds": 3600, "mr_url": ""}
    state.write(path, data)
    program = '''
import sys, time
from pathlib import Path
from types import SimpleNamespace
from podgrove import runtime
root, phase = Path(sys.argv[2]), sys.argv[3]
def block(*args):
    (root / "blocked").write_text(phase)
    time.sleep(60)
noop = lambda *args: None
runtime.Kube = lambda *args, **kwargs: SimpleNamespace(wait=block if phase == "startup" else noop,
                                           heartbeat=block if phase == "heartbeat" else noop)
config = SimpleNamespace(root=root, forward=[], ttl_seconds=3600, network={})
runtime.load_config = lambda *args: config
runtime.Compose = lambda *args: SimpleNamespace(model=lambda: {"services": {"app": {}}}, validate=noop,
                                               published_ports=lambda *args: [], has_watch=lambda *args: False)
sync = SimpleNamespace(sync_once=lambda: 0, cancel=noop, close=noop)
rows = [{"Service": "app", "State": "running"}]
runtime.launch_stack = lambda *args, **kwargs: (sync, .01, rows)
runtime.service_status = block
runtime.HEALTH_INTERVAL = 0
tunnel = SimpleNamespace(check=noop, close=noop, snapshot=lambda: {"verification": {"state": "verified"}})
runtime.DockerTunnel = lambda *args: SimpleNamespace(start=lambda: tunnel)
runtime.run = lambda *args, **kwargs: None
raise SystemExit(runtime.serve(Path(sys.argv[1])))
'''
    process = subprocess.Popen([sys.executable, "-c", program, str(path), str(root), blocked_phase],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                if (root / "blocked").exists():
                    expected = "starting" if blocked_phase == "startup" else "ready"
                    assert runtime.control(data, "ping")["status"] == expected
                    break
            except PodgroveError:
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert runtime.control(data, "stop")["ok"]
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stdout + stderr
        assert state.read(path)["status"] == "disconnected"
        assert not socket_path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        socket_path.unlink(missing_ok=True)
