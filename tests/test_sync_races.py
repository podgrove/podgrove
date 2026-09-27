"""Local snapshot races can be retried; remotely uncertain batches cannot."""
import errno
import io
import subprocess
import sys
import tarfile
import threading
import time
from unittest.mock import Mock

import pytest

from podgrove.errors import PodgroveError
from podgrove.runtime import SyncWorker
from podgrove.sync import SnapshotRace
from test_sync import FakeSynchronizer


@pytest.fixture
def mirror(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("initial contents")
    sync = FakeSynchronizer(tmp_path, [source])
    sync.start()
    yield sync, source
    sync.close()


def test_independent_writer_replaces_source_before_archive_then_only_stable_batch_is_sent(mirror, monkeypatch):
    sync, source = mirror
    source.write_text("first edit")
    before = sync._baseline.copy()
    original = sync._add_file
    calls = []
    def race(tar, relative, entry):
        calls.append(relative)
        if len(calls) == 1:
            subprocess.run([sys.executable, "-c",
                            "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                            "q=p.with_suffix('.saving'); q.write_text('stable atomic save'); q.replace(p)",
                            str(source)], check=True, timeout=2)
            assert sync._baseline == before and len(sync.transfers) == 1
        return original(tar, relative, entry)
    monkeypatch.setattr(sync, "_add_file", race)
    assert sync.sync_once() == 1
    assert len(calls) == 2 and len(sync.transfers) == 2
    assert sync.transfers[-1]["podgrove-transfer/payload/source.txt"] == b"stable atomic save"


def test_truncate_during_tar_read_is_local_race_not_uncertain_remote_failure(mirror, monkeypatch):
    sync, source = mirror
    source.write_text("edit" * 10000)
    original = tarfile.copyfileobj
    truncated = []
    def copy(source_stream, destination, *args, **kwargs):
        if isinstance(source_stream, io.BufferedReader) and not truncated:
            source.write_text("stable shorter edit")
            truncated.append(True)
        return original(source_stream, destination, *args, **kwargs)
    monkeypatch.setattr(tarfile, "copyfileobj", copy)
    assert sync.sync_once() == 1
    assert truncated and len(sync.transfers) == 2
    assert sync.transfers[-1]["podgrove-transfer/payload/source.txt"] == b"stable shorter edit"


def test_continued_churn_has_bounded_per_call_retries_and_no_partial_baseline(mirror, monkeypatch):
    sync, source = mirror
    source.write_text("edited")
    before = sync._baseline.copy()
    remote = sync.remote_baseline
    archive = Mock(side_effect=SnapshotRace("source still changing"))
    monkeypatch.setattr(sync, "_add_file", archive)
    started = time.monotonic()
    with pytest.raises(SnapshotRace):
        sync.sync_once()
    assert archive.call_count == 3 and time.monotonic() - started < 1
    assert sync._baseline == before and sync.remote_baseline == remote and len(sync.transfers) == 1


def test_cancel_interrupts_snapshot_backoff_before_retry(mirror, monkeypatch):
    sync, source = mirror
    source.write_text("edited")
    def cancel(*_):
        sync.cancel()
        raise SnapshotRace("changed")
    archive = Mock(side_effect=cancel)
    monkeypatch.setattr(sync, "_add_file", archive)
    started = time.monotonic()
    with pytest.raises(PodgroveError, match="cancelled"):
        sync.sync_once()
    assert time.monotonic() - started < .1
    assert archive.call_count == 1 and len(sync.transfers) == 1


def test_permissions_are_permanent_not_snapshot_retry(mirror, monkeypatch):
    sync, source = mirror
    source.write_text("edited")
    read = Mock(side_effect=PermissionError(errno.EACCES, "denied"))
    monkeypatch.setattr(sync, "_open_file", read)
    with pytest.raises(PodgroveError, match="Cannot read") as caught:
        sync.sync_once()
    assert not isinstance(caught.value, SnapshotRace)
    assert read.call_count == 1 and len(sync.transfers) == 1


def test_remote_uncertain_ack_is_never_retried_by_snapshot_recovery(mirror, monkeypatch):
    sync, source = mirror
    source.write_text("edited")
    before = sync._baseline.copy()
    transfer = Mock(side_effect=PodgroveError("remote closed before acknowledging batch"))
    monkeypatch.setattr(sync, "_send_archive", transfer)
    with pytest.raises(PodgroveError, match="acknowledging"):
        sync.sync_once()
    assert transfer.call_count == 1 and sync._baseline == before


def test_worker_keeps_source_churn_recoverable_and_reports_recovery():
    sync = Mock()
    sync.sync_once.side_effect = [SnapshotRace("active editor"), SnapshotRace("active editor"), 1, 0]
    activity = Mock()
    activity.changed.return_value = False
    events = []
    recovered = threading.Event()
    def status(value):
        events.append(value)
        if len(events) > 1 and value["state"] == "ready":
            recovered.set()
    worker = SyncWorker(sync, activity, interval=.01, on_status=status).start()
    try:
        assert recovered.wait(1)
        assert worker.snapshot() > 0
        assert [item["state"] for item in events] == ["retrying", "ready"]
        assert events[1]["checked_at"] >= events[0]["checked_at"]
    finally:
        worker.close()
    sync.close.assert_not_called()


def test_worker_does_not_retry_unknown_remote_error():
    sync = Mock()
    sync.sync_once.side_effect = PodgroveError("remote ACK is uncertain")
    activity = Mock()
    activity.changed.return_value = False
    worker = SyncWorker(sync, activity, interval=.01).start()
    try:
        worker.thread.join(timeout=1)
        with pytest.raises(PodgroveError, match="uncertain"):
            worker.snapshot()
        assert sync.sync_once.call_count == 1
    finally:
        worker.close()


def test_source_replaced_with_fifo_between_stat_and_open_never_blocks(mirror, monkeypatch):
    import os
    sync, source = mirror
    source.write_text("edited")
    original = sync._open_file
    def replace(path):
        source.unlink()
        os.mkfifo(source)
        return original(path)
    monkeypatch.setattr(sync, "_open_file", replace)
    started = time.monotonic()
    with pytest.raises(PodgroveError, match="regular files"):
        sync.sync_once()
    assert time.monotonic() - started < .2 and len(sync.transfers) == 1
