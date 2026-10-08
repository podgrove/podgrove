"""Mirror unit coverage and optional real Docker transport/inode checks.

Run the disposable Docker checks with PODGROVE_DOCKER_TESTS=1 pytest
 tests/test_sync.py. They use uniquely named Docker volumes, never host binds.
"""
from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import sys
import threading
import time
import uuid

import pytest

from podgrove.errors import PodgroveError
from podgrove.sync import Synchronizer


class FakeSynchronizer(Synchronizer):
    def __init__(self, root, paths, **kwargs):
        self.calls = []
        self.transfers = []
        self.transfer_metadata = []
        self.container_exists = False
        self.volume_exists = False
        self.remote_baseline = b""
        self.foreign = False
        self.mirror_empty = False
        super().__init__(root, paths, {}, kwargs.get("identity", "unit-test"), exclude=kwargs.get("exclude"))

    def _start_receiver(self, **_kwargs):
        pass  # Real framed receiver behavior is covered by the Docker checks.

    def _send_archive(self, archive):
        # Unit tests inspect archive contents; real Docker tests below exercise
        # the framed persistent receiver and unchanged shell apply program.
        from podgrove.sync import _RECEIVE, _APPLY
        self._docker("exec", "-i", self.container, "sh", "-c", _RECEIVE + _APPLY, stdin=archive)

    def _docker(self, *args, stdin=None, check=True, timeout=300):
        self.calls.append(args)
        output = b""
        if args[:2] in (("container", "inspect"), ("volume", "inspect")):
            exists = self.container_exists if args[0] == "container" else self.volume_exists
            if not exists:
                return subprocess.CompletedProcess(args, 1, b"", b"No such object")
            output = json.dumps({} if self.foreign else self.labels).encode()
        elif args[:2] == ("volume", "create"):
            self.volume_exists = True
        elif args[0] == "run":
            self.container_exists = True
            output = b"a" * 64 + b"\n"
        elif args[0] == "rm":
            self.container_exists = False
        elif args[0] == "exec" and stdin is not None:
            with tarfile.open(fileobj=io.BytesIO(stdin.read())) as archive:
                self.transfer_metadata.append({item.name: (item.uid, item.gid, item.mode)
                                               for item in archive.getmembers()})
                transferred = {item.name: archive.extractfile(item).read() if item.isfile() else None
                               for item in archive.getmembers()}
            self.transfers.append(transferred)
            self.remote_baseline = transferred["podgrove-transfer/control/baseline.json"]
        elif args[0] == "exec" and args[-1].startswith("if [ -f /metadata/baseline.json"):
            output = self.remote_baseline
        elif args[0] == "exec" and "echo empty" in args[-1] and self.mirror_empty:
            output = b"empty\n"
        return subprocess.CompletedProcess(args, 0, output, b"")


def control(sync, name):
    return sync.transfers[-1][f"podgrove-transfer/control/{name}"].decode().split("\0")[:-1]


def test_initial_payload_and_noop_exclude_git_and_unrequested_siblings(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "main.py").write_text("hello")
    (app / ".git").write_text("gitdir: /private/repo")
    nested = app / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    (nested / ".git" / "secret").write_text("excluded")
    (tmp_path / "sibling").write_text("not requested")
    sync = FakeSynchronizer(tmp_path, [app, app / "main.py"])
    assert sync.paths == [app]
    sync.start()
    payload = sync.transfers[-1]
    assert payload["podgrove-transfer/payload/app/main.py"] == b"hello"
    info = (app / "main.py").stat()
    assert sync.transfer_metadata[-1]["podgrove-transfer/payload/app/main.py"][:2] == (info.st_uid, info.st_gid)
    assert sync.transfer_metadata[-1]["podgrove-transfer/payload/app"][:2] == (app.stat().st_uid, app.stat().st_gid)
    transfers = [args for args in sync.calls if args[:2] == ("exec", "-i")]
    assert len(transfers) == 1 and "tar -xpf -" in transfers[0][-1]
    assert not any(".git" in path or "sibling" in path for path in payload)
    assert sync.sync_once() == 0
    assert len(sync.transfers) == 1
    run = next(args for args in sync.calls if args[0] == "run")
    assert f"{tmp_path}:/workspace" in run
    assert "none" in run
    sync.close()
    assert not sync.container_exists
    assert sync.volume_exists  # baseline is deliberately retained until environment deletion


def test_single_file_edits_modes_rename_and_deletion(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    source = app / "old.py"
    source.write_text("old")
    sync = FakeSynchronizer(tmp_path, [app])
    sync.start()
    source.write_text("new")
    assert sync.sync_once() == 1
    assert control(sync, "files") == ["app/old.py"]
    source.chmod(0o755)
    assert sync.sync_once() == 1
    source.rename(app / "new.py")
    assert sync.sync_once() == 2
    assert control(sync, "deleted") == ["app/old.py"]
    assert control(sync, "files") == ["app/new.py"]
    (app / "new.py").unlink()
    app.rmdir()
    assert sync.sync_once() == 2
    assert control(sync, "deleted") == ["app/new.py", "app"]


@pytest.mark.parametrize("directory_mode,file_mode", [(0o700, 0o600), (0o750, 0o640), (0o770, 0o751)])
def test_initial_sync_normalizes_remote_permissions_without_changing_sources(tmp_path, directory_mode, file_mode):
    directory = tmp_path / "shared" / "middleware"
    directory.mkdir(parents=True)
    source = directory / "module.py"
    source.write_text("value = 42")
    for path in (tmp_path, directory.parent, directory):
        path.chmod(directory_mode)
    source.chmod(file_mode)
    sync = FakeSynchronizer(tmp_path, [source])
    sync.start()
    metadata = sync.transfer_metadata[-1]
    for relative in (".", "shared", "shared/middleware"):
        assert metadata[f"podgrove-transfer/payload/{relative}"][2] == directory_mode | 0o755
        assert (tmp_path / relative).stat().st_mode & 0o7777 == directory_mode
    assert metadata["podgrove-transfer/payload/shared/middleware/module.py"][2] == file_mode | 0o644
    assert source.stat().st_mode & 0o7777 == file_mode
    assert sync.sync_once() == 0


def test_incremental_sync_normalizes_new_paths_and_chmod_only_changes(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("value = 1")
    sync = FakeSynchronizer(tmp_path, [tmp_path])
    sync.start()
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    child = directory / "secret.py"
    child.write_text("value = 2")
    child.chmod(0o600)
    source.chmod(0o700)
    sync.sync_once()
    metadata = sync.transfer_metadata[-1]
    assert metadata["podgrove-transfer/payload/private"][2] == 0o755
    assert metadata["podgrove-transfer/payload/private/secret.py"][2] == 0o644
    assert metadata["podgrove-transfer/payload/app.py"][2] == 0o744
    directory.chmod(0o750)
    child.chmod(0o740)
    source.chmod(0o600)
    assert sync.sync_once() == 3
    metadata = sync.transfer_metadata[-1]
    assert metadata["podgrove-transfer/payload/private"][2] == 0o755
    assert metadata["podgrove-transfer/payload/private/secret.py"][2] == 0o744
    assert metadata["podgrove-transfer/payload/app.py"][2] == 0o644
    assert sync.sync_once() == 0


def test_reconnect_repairs_old_private_modes_without_resending_unchanged_contents(tmp_path):
    tmp_path.chmod(0o755)
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o700)
    source = directory / "module.py"
    source.write_text("original local contents")
    source.chmod(0o600)
    first = FakeSynchronizer(tmp_path, [directory])
    first.start()
    saved = json.loads(first.remote_baseline)
    for entry in saved["entries"].values():
        entry.pop("remote_mode", None)
    second = FakeSynchronizer(tmp_path, [directory])
    second.remote_baseline = json.dumps(saved).encode()
    second.start()
    assert control(second, "files") == []
    assert control(second, "file_modes") == ["shared/module.py"]
    assert control(second, "directory_modes") == ["shared"]
    assert second.transfers[-1]["podgrove-transfer/payload/shared/module.py"] == b""
    assert second.transfer_metadata[-1]["podgrove-transfer/payload/shared/module.py"][2] == 0o644
    assert second.transfer_metadata[-1]["podgrove-transfer/payload/shared"][2] == 0o755
    assert second.sync_once() == 0


def test_reconnect_keeps_an_already_readable_old_mirror_unchanged(tmp_path):
    tmp_path.chmod(0o755)
    source = tmp_path / "module.py"
    source.write_text("original local contents")
    source.chmod(0o644)
    first = FakeSynchronizer(tmp_path, [source])
    first.start()
    saved = json.loads(first.remote_baseline)
    for entry in saved["entries"].values():
        entry.pop("remote_mode", None)
    second = FakeSynchronizer(tmp_path, [source])
    second.remote_baseline = json.dumps(saved).encode()
    second.start()
    assert second.transfers == []


def test_real_apply_normalizes_modes_and_upgrades_without_overwriting_remote_edits(tmp_path, monkeypatch):
    from podgrove.sync import _APPLY, _RECEIVE
    gnu_stat = shutil.which("gstat") or (shutil.which("stat") if sys.platform.startswith("linux") else None)
    if not gnu_stat:
        pytest.skip("GNU stat is required to run the production Linux apply script locally")
    local, remote, metadata, stage = (tmp_path / name for name in ("local", "remote", "metadata", "stage"))
    for path in (local, remote, metadata):
        path.mkdir()
        os.chown(path, -1, os.getgid())
    directory = local / "middleware"
    directory.mkdir(mode=0o700)
    source = directory / "module.py"
    source.write_text("initial")
    source.chmod(0o600)
    script = (_RECEIVE + _APPLY).replace("/tmp/podgrove-transfer", "${TEST_STAGE}")
    script = script.replace("/tmp/podgrove-incoming.", "${TEST_METADATA}/podgrove-incoming.")
    script = script.replace("/workspace", "${TEST_WORKSPACE}").replace("/metadata", "${TEST_METADATA}")
    script = script.replace("!= ${TEST_WORKSPACE}", '!= "${TEST_WORKSPACE}"')
    script = script.replace("stat -c", shlex.quote(gnu_stat) + " -c")
    env = dict(os.environ, TEST_STAGE=str(stage), TEST_WORKSPACE=str(remote), TEST_METADATA=str(metadata))

    def apply_with(sync):
        send = sync._send_archive
        def send_and_apply(archive):
            content = archive.read()
            archive.seek(0)
            result = subprocess.run(["bash", "-c", script], input=content, capture_output=True, env=env, timeout=10)
            assert result.returncode == 0, result.stderr.decode()
            send(archive)
        monkeypatch.setattr(sync, "_send_archive", send_and_apply)

    first = FakeSynchronizer(local, [directory])
    apply_with(first)
    first.start()
    target = remote / "middleware" / "module.py"
    assert target.read_text() == "initial"
    assert target.stat().st_mode & 0o777 == 0o644
    assert target.parent.stat().st_mode & 0o777 == 0o755
    inode = target.stat().st_ino
    target.write_text("retained remote edit")
    target.chmod(0o600)
    target.parent.chmod(0o700)
    saved = json.loads(first.remote_baseline)
    for entry in saved["entries"].values():
        entry.pop("remote_mode", None)
    resumed = FakeSynchronizer(local, [directory])
    resumed.remote_baseline = json.dumps(saved).encode()
    apply_with(resumed)
    resumed.start()
    assert target.read_text() == "retained remote edit"
    assert target.stat().st_ino == inode
    assert target.stat().st_mode & 0o777 == 0o644
    assert target.parent.stat().st_mode & 0o777 == 0o755
    assert source.stat().st_mode & 0o777 == 0o600
    source.write_text("local update")
    source.chmod(0o700)
    resumed.sync_once()
    assert target.read_text() == "local update"
    assert target.stat().st_ino == inode
    assert target.stat().st_mode & 0o777 == 0o744
    assert source.stat().st_mode & 0o777 == 0o700


def test_failed_streamed_apply_keeps_baseline_and_next_edit_can_retry(tmp_path, monkeypatch):
    source = tmp_path / "file"
    source.write_text("before")
    sync = FakeSynchronizer(tmp_path, [source])
    sync.start()
    baseline, remote = sync._baseline.copy(), sync.remote_baseline
    real_docker = sync._docker
    transfers = []

    def fail_apply(*args, stdin=None, **kwargs):
        if stdin is not None:
            transfers.append(args)
            raise PodgroveError("streamed tar application failed")
        return real_docker(*args, stdin=stdin, **kwargs)

    source.write_text("after")
    monkeypatch.setattr(sync, "_docker", fail_apply)
    with pytest.raises(PodgroveError, match="application failed"):
        sync.sync_once()
    assert sync._baseline == baseline and sync.remote_baseline == remote
    assert len(transfers) == 1 and transfers[0][:2] == ("exec", "-i")
    monkeypatch.setattr(sync, "_docker", real_docker)
    assert sync.sync_once() == 1
    assert sync.transfers[-1]["podgrove-transfer/payload/file"] == b"after"


def test_missing_single_file_parent_after_start_is_tracked_deletion(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    source = directory / "dev.env"
    source.write_text("MODE=dev")
    sync = FakeSynchronizer(tmp_path, [source])
    sync.start()
    source.unlink()
    directory.rmdir()
    assert sync.sync_once() == 2
    assert control(sync, "deleted") == ["config/dev.env", "config"]


def test_file_to_directory_and_directory_to_file(tmp_path):
    source = tmp_path / "data"
    source.write_text("file")
    sync = FakeSynchronizer(tmp_path, [source])
    sync.start()
    source.unlink()
    source.mkdir()
    (source / "nested").write_text("child")
    assert sync.sync_once() == 2
    assert control(sync, "deleted") == ["data"]
    assert "data" in control(sync, "directories")
    shutil.rmtree(source)
    source.write_text("file again")
    assert sync.sync_once() == 2
    assert control(sync, "deleted") == ["data/nested", "data"]


def test_special_characters_remain_data(tmp_path):
    filename = "space ' quote $() `backtick`\n雪.txt"
    (tmp_path / filename).write_text("safe")
    sync = FakeSynchronizer(tmp_path, [tmp_path])
    sync.start()
    assert control(sync, "files") == [filename]
    assert sync.transfers[-1][f"podgrove-transfer/payload/{filename}"] == b"safe"
    assert all(filename not in arg for call in sync.calls for arg in call)


def test_reconnect_loads_baseline_and_retains_remote_changes(tmp_path):
    source = tmp_path / "file"
    source.write_text("initial")
    first = FakeSynchronizer(tmp_path, [tmp_path])
    first.start()
    first.close()
    second = FakeSynchronizer(tmp_path, [tmp_path])
    second.remote_baseline = first.remote_baseline
    second.volume_exists = True
    second.start()
    assert second.transfers == []
    source.write_text("edited")
    assert second.sync_once() == 1


def test_empty_mirror_after_daemon_recreation_is_repopulated(tmp_path):
    source = tmp_path / "file"
    source.write_text("initial")
    first = FakeSynchronizer(tmp_path, [tmp_path])
    first.start()
    second = FakeSynchronizer(tmp_path, [tmp_path])
    second.remote_baseline = first.remote_baseline
    second.volume_exists = True
    second.mirror_empty = True
    second.start()
    assert control(second, "files") == ["file"]


def test_config_scope_removal_does_not_delete_remote_previous_sources(tmp_path):
    (tmp_path / "first").write_text("first")
    (tmp_path / "second").write_text("second")
    first = FakeSynchronizer(tmp_path, [tmp_path])
    first.start()
    (tmp_path / "first").write_text("changed")
    second = FakeSynchronizer(tmp_path, [tmp_path / "first"])
    second.remote_baseline = first.remote_baseline
    second.start()
    assert control(second, "deleted") == []
    assert control(second, "files") == ["first"]


@pytest.mark.parametrize("source", ["missing", "../outside", ".git"])
def test_invalid_sources_fail_before_docker(tmp_path, source):
    with pytest.raises(PodgroveError, match="does not exist|outside worktree|.git"):
        FakeSynchronizer(tmp_path, [tmp_path / source])


def test_symlinks_are_refused_even_inside_root(tmp_path):
    (tmp_path / "target").write_text("value")
    (tmp_path / "link").symlink_to("target")
    with pytest.raises(PodgroveError, match="Symlinks"):
        FakeSynchronizer(tmp_path, [tmp_path])
    with pytest.raises(PodgroveError, match="Symlinks"):
        FakeSynchronizer(tmp_path, [tmp_path / "link"])


def test_special_file_is_refused(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(PodgroveError, match="regular files"):
        FakeSynchronizer(tmp_path, [tmp_path])


def test_symlink_added_after_start_refused_before_mutation(tmp_path):
    (tmp_path / "initial").write_text("value")
    sync = FakeSynchronizer(tmp_path, [tmp_path])
    sync.start()
    calls = len(sync.calls)
    (tmp_path / "link").symlink_to("initial")
    with pytest.raises(PodgroveError, match="Symlinks"):
        sync.sync_once()
    assert len(sync.calls) == calls


def test_unowned_helper_or_volume_is_never_removed(tmp_path):
    sync = FakeSynchronizer(tmp_path, [tmp_path])
    sync.container_exists = True
    sync.foreign = True
    with pytest.raises(PodgroveError, match="unowned sync container"):
        sync.start()
    assert not any(args[0] == "rm" for args in sync.calls)


def test_no_sources_does_not_create_helper(tmp_path):
    sync = FakeSynchronizer(tmp_path, [])
    sync.start()
    assert sync.sync_once() == 0
    sync.close()
    assert sync.calls == []


def test_unstarted_sync_is_an_actionable_error(tmp_path):
    sync = FakeSynchronizer(tmp_path, [])
    with pytest.raises(PodgroveError, match="not been started"):
        sync.sync_once()


def test_docker_timeout_is_actionable(tmp_path, monkeypatch):
    sync = Synchronizer(tmp_path, [], os.environ.copy(), "timeout")
    from unittest.mock import Mock
    child = Mock()
    child.communicate.side_effect = [subprocess.TimeoutExpired("docker version", 300), (b"", b"")]
    child.poll.return_value = None
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=child))
    with pytest.raises(PodgroveError, match="Docker file sync failed"):
        sync._docker("version")
    child.kill.assert_called_once()
    assert child.communicate.call_count == 2
    assert sync._process is None


def test_invalid_remote_baseline_closes_created_helper(tmp_path):
    (tmp_path / "file").write_text("hello")
    sync = FakeSynchronizer(tmp_path, [tmp_path])
    sync.remote_baseline = b'{"version": 99}'
    with pytest.raises(PodgroveError, match="Invalid remote sync baseline"):
        sync.start()
    assert not sync.container_exists


def test_sync_cleanup_failure_preserves_primary_startup_error(tmp_path, monkeypatch, capsys):
    (tmp_path / "file").write_text("hello")
    sync = FakeSynchronizer(tmp_path, [tmp_path])
    sync.remote_baseline = b'{"version": 99}'

    def failed_cleanup():
        raise PodgroveError("secondary inspect reset")

    monkeypatch.setattr(sync, "close", failed_cleanup)
    with pytest.raises(PodgroveError, match="Invalid remote sync baseline"):
        sync.start()
    assert "secondary inspect reset" in capsys.readouterr().err


def test_cleanup_never_removes_same_name_replacement_when_captured_helper_is_gone(tmp_path, monkeypatch):
    (tmp_path / "file").write_text("initial")
    sync = FakeSynchronizer(tmp_path, [tmp_path])
    sync.start()
    inspected = []
    def labels(kind, name):
        inspected.append((kind, name))
        return None if name == "a" * 64 else sync.labels
    monkeypatch.setattr(sync, "_inspect_labels", labels)
    before = len(sync.calls)
    sync.close()
    assert inspected == [("container", "a" * 64)]
    assert not any(call[0] == "rm" for call in sync.calls[before:])


@pytest.mark.parametrize("output", [b"", b"--all", b"a" * 63, b"a" * 64 + b"\nother-id"])
def test_invalid_created_helper_id_never_becomes_a_cleanup_target(tmp_path, monkeypatch, output):
    (tmp_path / "file").write_text("initial")
    sync = FakeSynchronizer(tmp_path, [tmp_path])
    original = sync._docker
    def docker(*args, **kwargs):
        result = original(*args, **kwargs)
        return subprocess.CompletedProcess(args, 0, output, b"") if args[0] == "run" else result
    monkeypatch.setattr(sync, "_docker", docker)
    with pytest.raises(PodgroveError, match="immutable file sync helper ID"):
        sync.start()
    assert sync._container_id is None and not sync._created
    assert not any(call[0] == "rm" for call in sync.calls)


def test_cancel_reaps_only_owned_transport_child_and_preserves_baseline(tmp_path):
    sync = Synchronizer(tmp_path, [], os.environ.copy(), "cancel-child")
    sync.docker = sys.executable
    sync._baseline = {"file": {"kind": "file", "digest": "previous-successful-transfer"}}
    errors = []

    def transfer():
        try:
            sync._docker("-c", "import time; time.sleep(60)")
        except PodgroveError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=transfer)
    thread.start()
    child = None
    try:
        deadline = time.monotonic() + 2
        while child is None and time.monotonic() < deadline:
            with sync._process_lock:
                child = sync._process
            time.sleep(0.01)
        assert child is not None
        started = time.monotonic()
        sync.cancel()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert time.monotonic() - started < 2
        assert child.poll() is not None
        assert errors == ["File synchronization cancelled"]
        assert sync._process is None
        assert sync._baseline["file"]["digest"] == "previous-successful-transfer"
        with pytest.raises(PodgroveError, match="cancelled"):
            sync._docker("-c", "raise SystemExit('must not run')")
    finally:
        sync.cancel()
        thread.join(timeout=3)


def test_sync_activity_is_recorded_before_transfer_starts(tmp_path, monkeypatch):
    source = tmp_path / "file"
    source.write_text("old")
    sync = FakeSynchronizer(tmp_path, [source])
    sync.start()
    before = dict(sync._baseline)
    events = []
    original = sync._docker

    def transfer(*args, **kwargs):
        if args[:2] == ("exec", "-i"):
            assert events == ["activity"]
            assert sync._baseline == before
            events.append("transfer")
        return original(*args, **kwargs)

    sync.activity_callback = lambda: events.append("activity")
    monkeypatch.setattr(sync, "_docker", transfer)
    source.write_text("new")
    assert sync.sync_once() == 1
    assert events == ["activity", "transfer"]


def test_lost_commit_ack_keeps_local_baseline_and_reconnect_loads_remote_commit(tmp_path, monkeypatch):
    source = tmp_path / "file"
    source.write_text("initial")
    sync = FakeSynchronizer(tmp_path, [source])
    sync.start()
    previous = dict(sync._baseline)
    send = sync._send_archive

    def committed_without_ack(archive):
        send(archive)  # The daemon committed, but the acknowledgement was lost.
        raise PodgroveError("closed before acknowledging its batch")

    monkeypatch.setattr(sync, "_send_archive", committed_without_ack)
    source.write_text("committed before disconnect")
    with pytest.raises(PodgroveError, match="before acknowledging"):
        sync.sync_once()
    assert sync._baseline == previous
    remote = sync.remote_baseline
    assert json.loads(remote)["entries"] != previous
    sync.close()
    resumed = FakeSynchronizer(tmp_path, [source])
    resumed.volume_exists = True
    resumed.remote_baseline = remote
    resumed.start()
    assert resumed._baseline == json.loads(remote)["entries"]
    assert resumed.transfers == [], "Reconnect must not replay a batch whose persisted baseline already committed"


_DOCKER_TESTS = os.environ.get("PODGROVE_DOCKER_TESTS") == "1"


class DisposableDockerSynchronizer(Synchronizer):
    """Exercise the real transport with an isolated volume, never a host path."""
    def __init__(self, root, paths, identity):
        super().__init__(root, paths, os.environ.copy(), identity)
        self.data_volume = f"{self.container}-test-data"

    def _docker(self, *args, **kwargs):
        if args[0] == "run" and "--volume" in args:
            args = list(args)
            position = args.index("--volume")
            assert args[position + 1] == f"{self.root}:/workspace"
            args[position:position + 2] = ["--mount", f"type=volume,source={self.data_volume},target=/workspace"]
        return super()._docker(*args, **kwargs)

    def cleanup(self):
        self.close()
        self._docker("volume", "rm", self.data_volume, self.volume, check=False)


@pytest.mark.integration
@pytest.mark.skipif(not _DOCKER_TESTS, reason="set PODGROVE_DOCKER_TESTS=1 for disposable Docker checks")
def test_docker_real_transport_inode_deletes_modes_and_remote_data(tmp_path):
    (tmp_path / "file").write_text("initial")
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "tracked").write_text("remove me")
    (directory / ".git").write_text("never copy")
    odd = "new\nline ' $() 雪"
    (tmp_path / odd).write_text("special")
    # Root-run Linux CI still exercises a non-root Compose user's write access.
    if os.getuid() == 0:
        for source in (tmp_path, tmp_path / "file", directory, directory / "tracked", tmp_path / odd):
            os.chown(source, 1000, 1000)
    tmp_path.chmod(0o700)
    directory.chmod(0o700)
    (tmp_path / "file").chmod(0o600)
    owner = f"{(tmp_path / 'file').stat().st_uid}:{(tmp_path / 'file').stat().st_gid}"
    assert not owner.startswith("0:")
    identity = "integration-" + uuid.uuid4().hex
    sync = DisposableDockerSynchronizer(tmp_path, [tmp_path], identity)
    consumer = sync.container + "-consumer"
    try:
        sync.start()
        assert sync._docker("exec", sync.container, "cat", "/workspace/file").stdout == b"initial"
        for path in ("/workspace", "/workspace/file", "/workspace/directory"):
            assert sync._docker("exec", sync.container, "stat", "-c", "%u:%g", path).stdout.strip() == owner.encode()
        for path in ("/workspace", "/workspace/directory"):
            assert sync._docker("exec", sync.container, "stat", "-c", "%a", path).stdout.strip() == b"755"
        assert sync._docker("exec", "--user", "65534:65534", sync.container,
                            "cat", "/workspace/file").stdout == b"initial"
        sync._docker("exec", "--user", owner, sync.container, "sh", "-c",
                     "test -w /workspace/file && echo owner-write > /workspace/directory/owner-created")
        assert sync._docker("exec", sync.container, "cat", "/workspace/" + odd).stdout == b"special"
        assert sync._docker("exec", sync.container, "test", "!", "-e", "/workspace/directory/.git").returncode == 0
        mountpoint = sync._docker("volume", "inspect", "--format", "{{.Mountpoint}}",
                                 sync.data_volume).stdout.decode().strip()
        sync._docker("run", "--detach", "--name", consumer, "--mount",
                     f"type=bind,source={mountpoint}/file,target=/observed", "alpine:3.21", "sleep", "300")
        before_inode = sync._docker("exec", consumer, "stat", "-c", "%i", "/observed").stdout
        (tmp_path / "file").write_text("edited")
        (tmp_path / "file").chmod(0o751)
        sync.sync_once()
        assert sync._docker("exec", consumer, "cat", "/observed").stdout == b"edited"
        assert sync._docker("exec", consumer, "stat", "-c", "%i", "/observed").stdout == before_inode
        assert sync._docker("exec", consumer, "stat", "-c", "%a", "/observed").stdout.strip() == b"755"
        assert sync._docker("exec", consumer, "stat", "-c", "%u:%g", "/observed").stdout.strip() == owner.encode()
        sync._docker("exec", sync.container, "sh", "-c", "echo retained > /workspace/directory/remote-data")
        shutil.rmtree(directory)
        sync.sync_once()
        assert sync._docker("exec", sync.container, "cat", "/workspace/directory/remote-data").stdout == b"retained\n"
        assert sync._docker("exec", sync.container, "test", "!", "-e", "/workspace/directory/tracked").returncode == 0
        sync._docker("exec", sync.container, "sh", "-c", "echo remote-edit > /workspace/file")
        sync.close()
        sync = DisposableDockerSynchronizer(tmp_path, [tmp_path], identity)
        sync.start()
        assert sync._docker("exec", sync.container, "cat", "/workspace/file").stdout == b"remote-edit\n"
        assert sync.sync_once() == 0
    finally:
        sync._docker("rm", "--force", consumer, check=False)
        sync.cleanup()


@pytest.mark.integration
@pytest.mark.skipif(not _DOCKER_TESTS, reason="set PODGROVE_DOCKER_TESTS=1 for disposable Docker checks")
def test_docker_remote_symlink_refusal_does_not_write_target(tmp_path):
    (tmp_path / "file").write_text("initial")
    sync = DisposableDockerSynchronizer(tmp_path, [tmp_path], "integration-" + uuid.uuid4().hex)
    try:
        sync.start()
        sync._docker("exec", sync.container, "sh", "-c",
                     "echo untouched > /tmp/target; rm /workspace/file; ln -s /tmp/target /workspace/file")
        (tmp_path / "file").write_text("edited")
        with pytest.raises(PodgroveError, match="Remote symlink blocks sync"):
            sync.sync_once()
        assert sync._docker("exec", sync.container, "cat", "/tmp/target").stdout == b"untouched\n"
    finally:
        sync.cleanup()


@pytest.mark.integration
@pytest.mark.skipif(not _DOCKER_TESTS, reason="set PODGROVE_DOCKER_TESTS=1 for disposable Docker checks")
def test_docker_truncated_transfer_never_applies_or_advances_remote_baseline(tmp_path):
    from podgrove.sync import _APPLY, _RECEIVE

    (tmp_path / "file").write_text("initial")
    sync = DisposableDockerSynchronizer(tmp_path, [tmp_path], "integration-" + uuid.uuid4().hex)
    try:
        sync.start()
        before = sync._docker("exec", sync.container, "cat", "/metadata/baseline.json").stdout
        idle_frame = sync._docker("exec", sync.container, "sh", "-c",
                                 "find /tmp -maxdepth 1 -name 'podgrove-*' -print").stdout
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            sync._add_bytes(tar, "podgrove-transfer/control/baseline.json", b"invalid baseline")
            sync._add_bytes(tar, "podgrove-transfer/payload/file", b"partial replacement" * 1024)
        truncated = io.BytesIO(archive.getvalue()[:2048])
        # subprocess needs a real file descriptor for streamed input.
        import tempfile
        with tempfile.TemporaryFile() as stream:
            stream.write(truncated.getvalue())
            stream.seek(0)
            result = sync._docker("exec", "-i", sync.container, "sh", "-c", _RECEIVE + _APPLY,
                                  stdin=stream, check=False)
        assert result.returncode != 0, result.stdout
        assert sync._docker("exec", sync.container, "cat", "/workspace/file").stdout == b"initial"
        assert sync._docker("exec", sync.container, "cat", "/metadata/baseline.json").stdout == before
        leftovers = sync._docker("exec", sync.container, "sh", "-c",
                                 "find /tmp -maxdepth 1 -name 'podgrove-*' -print").stdout
        assert leftovers == idle_frame  # The persistent receiver waits in one owned frame directory.
    finally:
        sync.cleanup()


@pytest.mark.integration
@pytest.mark.skipif(not _DOCKER_TESTS, reason="set PODGROVE_DOCKER_TESTS=1 for disposable Docker checks")
def test_persistent_docker_receiver_reuses_process_across_decimal_sequence_boundary(tmp_path):
    source = tmp_path / "file"
    source.write_text("initial")
    sync = DisposableDockerSynchronizer(tmp_path, [source], "integration-" + uuid.uuid4().hex)
    try:
        sync.start()
        process = sync._receiver.process
        for sequence in range(1, 13):
            source.write_text(f"edit {sequence}\n")
            assert sync.sync_once() == 1
            assert sync._receiver.process is process and process.poll() is None
        assert sync._receiver.sequence == 14
        assert sync._docker("exec", sync.container, "cat", "/workspace/file").stdout == b"edit 12\n"
    finally:
        sync.cleanup()


@pytest.mark.integration
@pytest.mark.skipif(not _DOCKER_TESTS, reason="set PODGROVE_DOCKER_TESTS=1 for disposable Docker checks")
def test_framed_truncation_after_tar_eof_never_applies_or_commits(tmp_path):
    from podgrove.sync import _APPLY, _RECEIVE
    from podgrove.sync_transport import RECEIVER
    import tempfile

    (tmp_path / "file").write_text("initial")
    sync = DisposableDockerSynchronizer(tmp_path, [tmp_path], "integration-" + uuid.uuid4().hex)
    try:
        sync.start()
        before = sync._docker("exec", sync.container, "cat", "/metadata/baseline.json").stdout
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for name, content in (("deleted", b""), ("directories", b".\0"), ("files", b"file\0"), ("file_modes", b""),
                                  ("directory_modes", b""), ("baseline.json", b"must not commit")):
                sync._add_bytes(tar, "podgrove-transfer/control/" + name, content)
            sync._add_bytes(tar, "podgrove-transfer/payload/file", b"must not apply")
        payload = archive.getvalue()
        assert payload[-512:] == b"\0" * 512
        nonce = uuid.uuid4().hex
        with tempfile.TemporaryFile() as stream:
            stream.write(f"PGS1 {nonce} {1:016d} {len(payload):016d}\n".encode())
            stream.write(payload[:-512])  # Tar itself accepts the remaining complete archive.
            stream.seek(0)
            result = sync._docker("exec", "-i", sync.container, "sh", "-c", RECEIVER,
                                  "test-frame", nonce, _RECEIVE + _APPLY, stdin=stream, check=False)
        assert result.returncode != 0 and b"Truncated sync frame body" in result.stderr
        assert b"ACK " not in result.stdout
        assert sync._docker("exec", sync.container, "cat", "/workspace/file").stdout == b"initial"
        assert sync._docker("exec", sync.container, "cat", "/metadata/baseline.json").stdout == before
    finally:
        sync.cleanup()


@pytest.mark.integration
@pytest.mark.skipif(not _DOCKER_TESTS, reason="set PODGROVE_DOCKER_TESTS=1 for disposable Docker checks")
def test_dead_persistent_helper_requires_reconnect_and_preserves_remote_only_content(tmp_path):
    (tmp_path / "file").write_text("initial")
    identity = "integration-" + uuid.uuid4().hex
    sync = DisposableDockerSynchronizer(tmp_path, [tmp_path], identity)
    resumed = None
    try:
        sync.start()
        sync._docker("exec", sync.container, "sh", "-c", "echo retained > /workspace/remote-only")
        sync._docker("rm", "--force", sync.container)
        sync._receiver.process.wait(timeout=5)
        with pytest.raises(PodgroveError, match="(exited|closed).*reconnect"):
            sync.sync_once()
        sync.close()
        resumed = DisposableDockerSynchronizer(tmp_path, [tmp_path], identity)
        resumed.start()
        receiver = resumed._receiver
        assert receiver is not None, "Unchanged reconnect must establish its stream before readiness"
        assert receiver.process.poll() is None and receiver.sequence == 1
        assert resumed._docker("exec", resumed.container, "cat", "/workspace/remote-only").stdout == b"retained\n"
        assert resumed.sync_once() == 0
        (tmp_path / "file").write_text("first edit after reconnect")
        assert resumed.sync_once() == 1
        assert resumed._receiver is receiver and receiver.sequence == 2
    finally:
        if resumed:
            resumed.cleanup()
        sync.cleanup()
