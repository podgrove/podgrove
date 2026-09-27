"""The binary acceptance harness detects truncation despite successful exit."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("check_exec_stream", Path(__file__).parents[1] / "scripts/check_exec_stream.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.mark.parametrize("size,code", [(29284, 0), (8 * 1024**2, 0), (29284, 7)])
def test_real_child_binary_and_stdin_eof(size, code, tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "command", lambda args, action, *suffix: [sys.executable, *suffix[3:]])
    args = SimpleNamespace(service="fixture", python="python3", timeout=10)
    result = probe.check_export(args, size, code, tmp_path)
    assert result["passed"] and result["received_bytes"] == size and result["exit"] == code
    assert not list(tmp_path.iterdir())  # Binary payload and stderr are not left behind.


def test_zero_exit_with_incomplete_output_is_failure(tmp_path, monkeypatch):
    script = "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(bytes(range(256)));sys.stderr.write('podgrove-exec-stderr-fixture')"
    monkeypatch.setattr(probe, "command", lambda *_: [sys.executable, "-c", script])
    args = SimpleNamespace(service="fixture", python="python3", timeout=10)
    result = probe.check_export(args, 29284, 0, tmp_path)
    assert not result["passed"] and result["exit"] == 0 and result["received_bytes"] == 256


def test_expected_digest_does_not_depend_on_read_chunk_boundaries(tmp_path):
    value = probe.PATTERN + probe.PATTERN[:137]
    output = tmp_path / "binary"
    output.write_bytes(value)
    size, digest = probe.digest_file(output)
    assert size == len(value) and digest == probe.expected_digest(size)


def test_periodic_identity_observation_timestamp_is_not_session_replacement(monkeypatch, tmp_path):
    data = {"identity": "012345abcdef", "context": "fixture", "namespace": "fixture", "status": "ready", "pid": 123,
            "engine_identity": {"state": "verified", "expected": {"pod_uid": "pod"},
                                "observed": {"pod_uid": "pod"}, "checked_at": 1}}
    monkeypatch.setattr(probe.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(data)))
    args = SimpleNamespace(binary=Path("/fixture/podgrove"), project_directory=tmp_path,
                           context="fixture", namespace="fixture", identity="012345abcdef")
    first = probe.status(args)
    data["engine_identity"]["checked_at"] = 99
    assert probe.status(args) == first
    data["engine_identity"]["observed"] = {"pod_uid": "replacement"}
    assert probe.status(args) != first
