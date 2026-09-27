import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import uuid

import pytest

from podgrove import state
from podgrove.errors import PodgroveError


@pytest.fixture
def local_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "worktree"
    root.mkdir()
    data = {"root": str(root), "context": "chosen-cluster", "identity": state.identity(root),
            "namespace": "default", "status": "disconnected"}
    path = state.state_path(root, data["context"])
    state.write(path, data)
    return path, data


def test_cleanup_removes_only_owned_environment_files_and_token_socket(local_state):
    path, data = local_state
    token = uuid.uuid4().hex
    socket_path = Path(tempfile.gettempdir()) / f"podgrove-{os.getuid()}-{token[:16]}.sock"
    data.update(token=token, socket=str(socket_path))
    owned = socket.socket(socket.AF_UNIX)
    owned.bind(str(socket_path))
    owned.close()
    state.write(path, data)
    path.with_suffix(".log").write_text("session log")
    path.with_suffix(".123.tmp").write_text("old temp")
    path.with_suffix(f".456.{uuid.uuid4().hex}.tmp").write_text("current temp")
    unrelated = path.parent / "unrelated.log"
    unrelated.write_text("keep")
    other_temp = path.with_suffix(".arbitrary.tmp")
    other_temp.write_text("keep")
    with state.lock(path):
        state.cleanup(path, data)
        assert path.with_suffix(".lock").exists()
    assert sorted(item.name for item in path.parent.iterdir()) == sorted([unrelated.name, other_temp.name])
    assert not socket_path.exists()


def test_empty_private_state_directory_is_removed_after_lock_release(local_state):
    path, data = local_state
    with state.lock(path):
        state.cleanup(path, data)
    assert not path.parent.exists()
    assert state.local_records(data["context"]) == []
    assert not path.parent.exists(), "listing must not recreate deleted local state"


def test_local_cleanup_keeps_retry_metadata_when_file_is_unsafe(local_state, tmp_path):
    path, data = local_state
    external = tmp_path / "foreign.log"
    external.write_text("keep")
    path.with_suffix(".log").symlink_to(external)
    with state.lock(path), pytest.raises(PodgroveError, match="unexpected local state"):
        state.cleanup(path, data)
    assert path.exists() and external.read_text() == "keep"
    assert not path.with_suffix(".lock").exists()


def test_local_cleanup_refuses_arbitrary_recorded_socket(local_state, tmp_path):
    path, data = local_state
    unrelated = tmp_path / "unrelated.sock"
    unrelated.write_text("keep")
    data.update(socket=str(unrelated), token=uuid.uuid4().hex)
    with state.lock(path), pytest.raises(PodgroveError, match="unrecognized session socket"):
        state.cleanup(path, data)
    assert path.exists() and unrelated.read_text() == "keep"


def test_inventory_verifies_context_filename_root_and_reports_corruption(local_state):
    path, data = local_state
    other = state.state_path(Path(data["root"]), "other-cluster")
    state.write(other, {**data, "context": "other-cluster"})
    corrupted = path.with_name("f" * 12 + "-" + path.name.split("-")[1])
    corrupted.write_text("not json")
    rows = state.local_records(data["context"])
    assert len(rows) == 2
    assert [(row["path"], row["data"]) for row in rows if "data" in row] == [(path, data)]
    assert [row["path"] for row in rows if "error" in row] == [corrupted]
    assert state.list_states(data["context"]) == [(path, data)]
    assert state.local_records(data["context"], "podgrove-testing") == [{"path": corrupted, "error": rows[-1]["error"]}]


def test_inventory_refuses_symlink_and_state_copied_under_another_identity(local_state):
    path, data = local_state
    copied = path.with_name("e" * 12 + "-" + path.name.split("-")[1])
    copied.write_text(json.dumps(data))
    link = path.with_name("f" * 12 + "-" + path.name.split("-")[1])
    link.symlink_to(path)
    records = state.local_records(data["context"])
    assert len([row for row in records if "error" in row]) == 2
    assert state.list_states(data["context"]) == [(path, data)]


def test_failed_atomic_write_leaves_no_temp_files(local_state):
    path, data = local_state
    original = path.read_bytes()
    with pytest.raises(TypeError):
        state.write(path, {"unserializable": object()})
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


def test_active_lock_cannot_be_acquired_and_is_removed_after_failure(local_state):
    path, _ = local_state
    with pytest.raises(RuntimeError, match="failure"):
        with state.lock(path):
            with pytest.raises(PodgroveError, match="Another podgrove"):
                with state.lock(path):
                    pytest.fail("Concurrent critical section")
            raise RuntimeError("failure")
    assert not path.with_suffix(".lock").exists()


def test_waiter_on_unlinked_old_inode_cannot_overlap_new_lock(local_state, monkeypatch):
    path, _ = local_state
    opened = threading.Event()
    resume = threading.Event()
    original = state.fcntl.flock
    intercepted = False
    errors = []
    critical = []

    def flock(stream, mode):
        nonlocal intercepted
        if threading.current_thread().name == "stale-lock-waiter" and mode & state.fcntl.LOCK_EX and not intercepted:
            intercepted = True
            opened.set()
            assert resume.wait(3)
        return original(stream, mode)

    def contender():
        try:
            with state.lock(path):
                critical.append("overlap")
        except PodgroveError as exc:
            errors.append(str(exc))

    monkeypatch.setattr(state.fcntl, "flock", flock)
    with state.lock(path):
        thread = threading.Thread(target=contender, name="stale-lock-waiter")
        thread.start()
        assert opened.wait(3)
    with state.lock(path):
        resume.set()
        thread.join(3)
        assert not thread.is_alive()
        assert critical == [] and len(errors) == 1
    assert not path.with_suffix(".lock").exists()
