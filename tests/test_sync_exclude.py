"""Opt-in mirror exclusions do not inspect or delete ignored generated files."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from podgrove import runtime
from podgrove.config import Config
from podgrove.errors import PodgroveError
from test_sync import FakeSynchronizer


def test_excluded_directories_are_pruned_before_stat_and_never_transferred(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.py").write_text("source")
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "compiled.pyc").write_text("generated")
    (source / "temporary.pyc").write_text("generated")
    real_lstat = Path.lstat
    def guarded(path, **kwargs):
        assert "__pycache__" not in path.parts and path.suffix != ".pyc", "Excluded path was inspected"
        return real_lstat(path, **kwargs)
    monkeypatch.setattr(Path, "lstat", guarded)
    sync = FakeSynchronizer(tmp_path, [source], exclude=["__pycache__", "*.pyc"])
    try:
        sync.start()
        assert sync.transfers[-1]["podgrove-transfer/payload/src/app.py"] == b"source"
        assert all("pyc" not in name for name in sync.transfers[-1])
        (cache / "new.pyc").write_text("new generated")
        assert sync.sync_once() == 0
    finally:
        sync.close()


def test_default_empty_excludes_preserve_compose_bind_parity(tmp_path):
    (tmp_path / "generated.pyc").write_text("intentional input")
    sync = FakeSynchronizer(tmp_path, [tmp_path])
    try:
        sync.start()
        assert sync.transfers[-1]["podgrove-transfer/payload/generated.pyc"] == b"intentional input"
    finally:
        sync.close()


def test_exclusions_on_reconnect_leave_previously_mirrored_files_untouched(tmp_path):
    generated = tmp_path / "generated.pyc"
    generated.write_text("old generated")
    source = tmp_path / "source.py"
    source.write_text("old source")
    initial = FakeSynchronizer(tmp_path, [tmp_path])
    initial.start()
    baseline = initial.remote_baseline
    initial.close()
    generated.write_text("ignored new generated")
    source.write_text("new source")
    resumed = FakeSynchronizer(tmp_path, [tmp_path], exclude=["*.pyc"])
    resumed.remote_baseline = baseline
    resumed.volume_exists = True
    try:
        resumed.start()
        payload = resumed.transfers[-1]
        assert payload["podgrove-transfer/control/deleted"] == b""
        assert "podgrove-transfer/payload/generated.pyc" not in payload
        assert payload["podgrove-transfer/payload/source.py"] == b"new source"
        assert "generated.pyc" not in resumed._baseline
    finally:
        resumed.close()


def test_explicitly_requested_ignored_bind_source_fails_before_remote_mutation(tmp_path):
    source = tmp_path / "generated.pyc"
    source.write_text("required bind input")
    with pytest.raises(PodgroveError, match="Explicit sync source matches"):
        FakeSynchronizer(tmp_path, [source], exclude=["*.pyc"])


def test_runtime_passes_only_explicit_configuration_patterns(tmp_path, monkeypatch):
    config = Config(root=tmp_path, files=[], sync_exclude=["__pycache__", "build/**"])
    compose = Mock(config=config)
    compose.sync_paths.return_value = [tmp_path]
    sync = Mock()
    factory = Mock(return_value=sync)
    monkeypatch.setattr(runtime, "Synchronizer", factory)
    monkeypatch.setattr(runtime, "run", Mock(return_value=Mock(stdout="", stderr="")))
    monkeypatch.setattr(runtime, "service_status", lambda *_, **_kwargs: [{"Service": "api", "State": "running", "Health": ""}])
    runtime.launch_stack(compose, {"services": {"api": {}}}, {}, "012345abcdef")
    factory.assert_called_once_with(tmp_path, [tmp_path], {}, "012345abcdef", exclude=config.sync_exclude)
