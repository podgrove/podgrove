"""Owned direct exec: real local stdio, bounded reads and immutable target fences."""

import json
import hashlib
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from podgrove import exec_transport as transport
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube

IDENT = "012345abcdef"
CONTAINER = "a" * 64


@pytest.fixture
def target(monkeypatch, tmp_path):
    sha256 = shutil.which("sha256sum") or shutil.which("gsha256sum")
    assert sha256, "Exec shell tests require coreutils sha256sum (gsha256sum on macOS)"
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "sha256sum").symlink_to(sha256)
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ.get("PATH", ""))
    labels = {MANAGED: "podgrove", ENVIRONMENT: IDENT}
    resources = {
        kind: {
            "metadata": {
                "name": "pg-" + IDENT + ("-0" if kind == "pod" else ""),
                "namespace": "team-dev",
                "uid": kind + "-uid",
                "labels": dict(labels),
            }
        }
        for kind in ("pod", "statefulset", "persistentvolumeclaim")
    }
    resources["pod"]["metadata"]["ownerReferences"] = [
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "name": "pg-" + IDENT,
            "uid": "statefulset-uid",
            "controller": True,
        }
    ]
    resources["pod"]["spec"] = {
        "volumes": [{"persistentVolumeClaim": {"claimName": "pg-" + IDENT}}],
        "containers": [
            {
                "name": "docker",
                "env": [
                    {
                        "name": "PODGROVE_POD_UID",
                        "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}},
                    }
                ],
            }
        ],
    }
    state = {
        "resources": resources,
        "ids": [CONTAINER],
        "observed_id": CONTAINER,
        "running": "true",
        "labels": {
            transport.PROJECT_LABEL: "sample-project",
            transport.SERVICE_LABEL: "api",
            transport.NUMBER_LABEL: "1",
            transport.ONEOFF_LABEL: "False",
        },
        "reads": [],
        "commands": [],
        "children": [],
        "script": "pass",
        "wire_script": None,
        "protocol": [],
    }

    def read(command, **_kwargs):
        state["reads"].append(command)
        if transport._ACK in command or transport._CLEANUP in command:
            state["protocol"].append("ack" if transport._ACK in command else "cleanup")
            program = transport._ACK if transport._ACK in command else transport._CLEANUP
            child = original(["sh", "-c", program, "fixture-protocol", *command[-2:]],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, error = child.communicate(timeout=3)
            assert child.returncode == 0, error
            return output
        if "get" in command:
            kind = command[command.index("get") + 1]
            return json.dumps(resources[kind]).encode()
        if "ps" in command:
            return "\n".join(state["ids"]).encode()
        assert "inspect" in command
        return (state["observed_id"] + "\n" + state["running"] + "\n" + json.dumps(state["labels"])).encode()

    original = subprocess.Popen

    def launch(command, **kwargs):
        if command[0] != "kubectl":
            child = original(command, **kwargs)
            state.setdefault("helpers", []).append(child)
            return child
        state["commands"].append((command, kwargs))
        # Replace only kubectl's executable with an inert local child. All
        # production command construction, stdio, monitoring and cleanup run.
        if state["wire_script"] is not None:
            child = original([sys.executable, "-u", "-c", state["wire_script"]], **kwargs)
        elif transport._STREAM_WRAPPER in command:
            nonce = command[command.index(transport._STREAM_WRAPPER) + 2]
            owner = command[command.index(transport._STREAM_WRAPPER) + 3]
            state["nonce"] = nonce
            child = original(["sh", "-c", transport._STREAM_WRAPPER, "fixture-stream", nonce, owner,
                              sys.executable, "-u", "-c", state["script"]], **kwargs)
        else:
            child = original([sys.executable, "-u", "-c", state["script"]], **kwargs)
        state["children"].append(child)
        return child

    monkeypatch.setattr(transport, "_read", read)
    monkeypatch.setattr(transport.subprocess, "Popen", launch)
    state["kube"] = Kube("explicit-cluster", "team-dev", namespace_mode="shared")
    return state


def execute(target, arguments=None, **kwargs):
    return transport.run_exec(
        target["kube"], IDENT, "sample-project", "api", arguments or ["sh", "-c", "true"], **kwargs
    )


def test_direct_exec_streams_binary_after_stdin_eof_and_preserves_exit_and_environment(target, monkeypatch):
    target["script"] = (
        "import sys;data=sys.stdin.buffer.read();sys.stdout.buffer.write(data*10000);sys.stderr.write('diagnostic');sys.exit(7)"
    )
    monkeypatch.setenv("KUBECTL_REMOTE_COMMAND_WEBSOCKETS", "false")
    with (
        tempfile.TemporaryFile() as input_file,
        tempfile.TemporaryFile() as output,
        tempfile.TemporaryFile() as error,
    ):
        input_file.write(b"\0\xffpayload\n")
        input_file.seek(0)
        assert (
            execute(
                target,
                ["python", "-c", "literal $HOME `argument`"],
                stdin=input_file,
                stdout=output,
                stderr=error,
            )
            == 7
        )
        output.seek(0)
        error.seek(0)
        assert output.read() == b"\0\xffpayload\n" * 10000
        assert error.read() == b"diagnostic"
    command, kwargs = target["commands"][0]
    assert command[:5] == ["kubectl", "--context", "explicit-cluster", "--namespace", "team-dev"]
    assert command[-6:] == ["exec", "-i", CONTAINER, "python", "-c", "literal $HOME `argument`"]
    assert "-t" not in command and "pod-uid" in command
    assert kwargs["env"]["KUBECTL_REMOTE_COMMAND_WEBSOCKETS"] == "true"
    assert kwargs["start_new_session"] is True
    assert len(target["commands"]) == 1 and all(p.poll() is not None for p in target["children"])
    assert not any(t.name == "podgrove-exec-ownership" for t in threading.enumerate())


def test_tty_flags_preserve_terminal_and_default_container_workdir(target):
    assert execute(target, tty=True, stdin=subprocess.DEVNULL) == 0
    command, kwargs = target["commands"][0]
    assert command.count("-t") == 2 and command.count("-i") == 2
    assert kwargs["start_new_session"] is False
    assert (
        "--workdir" not in command and "--user" not in command
    )  # Container configuration remains authoritative.


@pytest.mark.parametrize(
    "key,value",
    [
        (transport.PROJECT_LABEL, "other-project"),
        (transport.SERVICE_LABEL, "other-service"),
        (transport.NUMBER_LABEL, "2"),
        (transport.ONEOFF_LABEL, "True"),
    ],
)
def test_container_labels_are_independently_verified(target, key, value):
    target["labels"][key] = value
    with pytest.raises(PodgroveError, match="container ownership"):
        execute(target)
    assert not target["commands"]


@pytest.mark.parametrize("ids", [[], [CONTAINER, "b" * 64], ["container-name"], [CONTAINER + " --flag"]])
def test_absent_ambiguous_or_nonimmutable_container_is_refused(target, ids):
    target["ids"] = ids
    with pytest.raises(PodgroveError, match="exactly one"):
        execute(target)
    assert not target["commands"]


@pytest.mark.parametrize("kind", ["pod", "statefulset", "persistentvolumeclaim"])
@pytest.mark.parametrize("change", ["uid", "namespace", "label", "deleting"])
def test_foreign_or_unverifiable_resources_never_execute(target, kind, change):
    metadata = target["resources"][kind]["metadata"]
    if change == "uid":
        metadata.pop("uid")
    if change == "namespace":
        metadata["namespace"] = "foreign"
    if change == "label":
        metadata["labels"][ENVIRONMENT] = "foreign"
    if change == "deleting":
        metadata["deletionTimestamp"] = "deleting"
    with pytest.raises(PodgroveError):
        execute(target)
    assert not target["commands"]


@pytest.mark.parametrize("change", ["owner", "claim", "uid-env"])
def test_pod_owner_claim_and_downward_uid_binding_are_required(target, change):
    pod = target["resources"]["pod"]
    if change == "owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "other-controller"
    if change == "claim":
        pod["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "other-pvc"
    if change == "uid-env":
        pod["spec"]["containers"][0]["env"][0] = {"name": "PODGROVE_POD_UID", "value": "pod-uid"}
    with pytest.raises(PodgroveError):
        execute(target)
    assert not target["commands"]


def test_replacement_between_discovery_and_start_does_not_execute(target, monkeypatch):
    original = transport._container

    def replace(*args):
        selected = original(*args)
        target["resources"]["persistentvolumeclaim"]["metadata"]["uid"] = "replacement"
        return selected

    monkeypatch.setattr(transport, "_container", replace)
    with pytest.raises(PodgroveError, match="replaced during discovery"):
        execute(target)
    assert not target["commands"]


@pytest.mark.parametrize("kind", ["pod", "statefulset", "persistentvolumeclaim"])
def test_replacement_during_output_aborts_child_without_replay(target, monkeypatch, kind):
    target["script"] = "import os,time;os.write(1,b'partial-output\\n');time.sleep(30)"
    original = transport._engine

    def changed(*args, **kwargs):
        if kwargs.get("cancel") is not None:
            deadline = time.monotonic() + 3
            while output.tell() < len(b"partial-output\n") and time.monotonic() < deadline:
                if kwargs["cancel"].wait(.01):
                    return original(*args, **kwargs)
            target["resources"][kind]["metadata"]["uid"] = "replacement"
        return original(*args, **kwargs)

    monkeypatch.setattr(transport, "_engine", changed)
    with tempfile.TemporaryFile() as output:
        with pytest.raises(PodgroveError, match="not retried"):
            execute(target, stdout=output, verification_interval=0.05)
        output.seek(0)
        assert output.read() == b"partial-output\n"
    assert len(target["commands"]) == 1
    assert target["children"][0].poll() is not None


def test_exit_zero_cannot_mask_failed_final_ownership_check(target, monkeypatch):
    original = transport._engine
    calls = 0

    def unavailable(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PodgroveError("sensitive-discovery-error")
        return original(*args, **kwargs)

    monkeypatch.setattr(transport, "_engine", unavailable)
    with pytest.raises(PodgroveError, match="output may be incomplete") as error:
        execute(target)
    assert "sensitive" not in str(error.value) and len(target["commands"]) == 1


@pytest.mark.parametrize(
    "script,message",
    [
        ("import sys;sys.stdout.write('x'*70000)", "size limit"),
        ("import time;time.sleep(30)", "timed out"),
        ("import sys;sys.stderr.write('private-diagnostic');sys.exit(1)", "discovery failed"),
    ],
)
def test_metadata_reads_are_bounded_and_hide_stderr(script, message):
    with pytest.raises(PodgroveError, match=message) as error:
        transport._read([sys.executable, "-c", script], limit=65536, timeout=0.2)
    assert "private-diagnostic" not in str(error.value)


@pytest.mark.parametrize("close_stdout", [False, True])
def test_metadata_read_cancellation_reaps_its_process(monkeypatch, close_stdout):
    original = subprocess.Popen
    children = []

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    cancelled = threading.Event()
    timer = threading.Timer(0.15, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(PodgroveError, match="cancelled"):
            script = "import os,time;" + ("os.close(1);" if close_stdout else "") + "time.sleep(30)"
            transport._read([sys.executable, "-c", script], cancel=cancelled)
    finally:
        timer.join()
    assert children[0].poll() is not None
    assert time.monotonic() - started < 2


def test_cancelled_read_does_not_spawn_a_process(monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("must not spawn"))
    with pytest.raises(PodgroveError, match="cancelled"):
        transport._read([sys.executable, "-c", "pass"], cancel=cancelled)


def test_ownership_reads_allow_full_request_budget_and_cancel_before_next_resource(target, monkeypatch):
    original = transport._read
    cancelled = threading.Event()
    calls = []

    def read(command, **kwargs):
        calls.append(kwargs)
        result = original(command, **kwargs)
        cancelled.set()
        return result

    monkeypatch.setattr(transport, "_read", read)
    with pytest.raises(PodgroveError, match="cancelled"):
        transport._engine(target["kube"], IDENT, cancel=cancelled)
    assert len(calls) == 1
    assert calls[0]["timeout"] == transport.REQUEST_PROCESS_TIMEOUT


def test_invalid_target_does_not_run_discovery(monkeypatch):
    monkeypatch.setattr(transport, "_engine", lambda *_: pytest.fail("must not query"))
    for project in (None, "", "-other", "project;command"):
        with pytest.raises(PodgroveError, match="Invalid exec"):
            transport.run_exec(Kube("explicit-cluster", "team-dev"), IDENT, project, "api", ["true"])


@pytest.mark.parametrize("input_tty", [False, True])
@pytest.mark.parametrize("output_tty", [False, True])
@pytest.mark.parametrize("error_tty", [False, True])
def test_cli_exec_uses_native_tty_only_with_all_terminal_streams(
        tmp_path, monkeypatch, input_tty, output_tty, error_tty):
    from unittest.mock import Mock
    from podgrove import cli, runtime, state

    root = tmp_path / "worktree"
    root.mkdir()
    (root / "compose.yml").write_text("services: {api: {image: example/api}}\n")
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    ident = state.identity(root)
    state.write(state.state_path(root, "test-context"), {
        "identity": ident, "root": str(root), "context": "test-context", "namespace": "team-dev",
        "status": "ready", "compose_project": "recorded-project", "docker_host": "tcp://127.0.0.1:12345",
        "ports": [],
    })
    monkeypatch.setattr(runtime, "is_running", lambda _: True)
    monkeypatch.setattr(runtime, "control", lambda *_: {"ok": True, "status": "ready"})
    compose = Mock()
    compose.model.return_value = {"name": "ambient-project-must-not-select-target", "services": {"api": {}}}
    monkeypatch.setattr(cli, "Compose", lambda _: compose)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: input_tty)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: output_tty)
    monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: error_tty)
    run_exec = Mock(return_value=7)
    monkeypatch.setattr(transport, "run_exec", run_exec)
    args = cli.parser().parse_args(["exec", "--context", "test-context", "--namespace", "team-dev",
                                    "--project-directory", str(root), "api", "--", "python", "-c", "literal argument"])
    assert cli.execute(args) == 7
    assert run_exec.call_args.args[1:] == (ident, "recorded-project", "api", ["python", "-c", "literal argument"])
    assert run_exec.call_args.kwargs == {"tty": input_tty and output_tty and error_tty}
    compose.command.assert_not_called()


def test_discovery_timeout_cleans_helpers_after_kubectl_leader_exits(tmp_path):
    import os
    import signal
    marker = tmp_path / "auth-helper-pid"
    started = time.monotonic()
    try:
        with pytest.raises(PodgroveError, match="timed out"):
            transport._read([sys.executable, "-c",
                             "import os,pathlib,sys,time;pid=os.fork();"
                             "os._exit(0) if pid else None;"
                             "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)", str(marker)],
                            timeout=.3)
        assert time.monotonic() - started < 2
        helper = int(marker.read_text())
        status = subprocess.run(["ps", "-o", "stat=", "-p", str(helper)], capture_output=True, text=True, timeout=2)
        assert status.stdout.strip() in ("", "Z", "Z+")
    finally:
        if marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def frame(nonce, channel, payload, status=0):
    return (b'\x1ePODGROVE-EXEC:' + nonce.encode() + b':' + channel + b':'
            + f'{status:03d}:'.encode() + hashlib.sha256(payload).hexdigest().encode() + b'\x1f')


@pytest.mark.parametrize('split', [1, 7, 123, 4096])
def test_output_proof_preserves_binary_and_marker_shaped_application_bytes(split):
    nonce = 'b' * 32
    prefix = b'\x1ePODGROVE-EXEC:' + nonce.encode() + b':O:'
    payload = bytes(range(256)) * 8 + prefix + b'000:' + b'0' * 64 + b'\x1fmore\x00\xff'
    observed = []
    proof = transport._OutputProof(nonce, b'O', observed.append)
    wire = payload + frame(nonce, b'O', payload, 7)
    for offset in range(0, len(wire), split):
        proof.feed(wire[offset:offset + split])
        assert len(proof.pending) <= proof.trailer_size
    proof.finish()
    assert b''.join(observed) == payload and proof.status == 7


def test_output_proof_releases_valid_shaped_payload_before_completion_is_sealed():
    nonce = 'b' * 32
    first = b'abc'
    marker = frame(nonce, b'O', first)
    payload = first + marker + b'more'
    observed = []
    proof = transport._OutputProof(nonce, b'O', observed.append)
    proof.feed(first + marker)
    assert proof.status == 0
    proof.feed(b'more' + frame(nonce, b'O', payload, 7))
    proof.finish()
    assert proof.status == 7 and b''.join(observed) == payload
    proof.sealed = True
    with pytest.raises(PodgroveError, match='incomplete'):
        proof.feed(b'unexpected transport bytes')


def test_short_prompt_is_forwarded_before_application_waits_for_input():
    observed = []
    proof = transport._OutputProof('b' * 32, b'O', observed.append)
    proof.feed(b'Enter value: ')
    assert b''.join(observed) == b'Enter value: ' and not proof.pending


@pytest.mark.parametrize('mode', ['missing', 'truncated', 'corrupt', 'negative-exit'])
def test_successful_or_killed_transport_without_exact_completion_never_succeeds(target, mode):
    script = "import os,signal;os.write(1,b'partial-binary\\x00\\xff');"
    if mode == 'truncated':
        script += "os.write(2,b'\\x1ePODGROVE-EXEC:truncated');"
    elif mode == 'corrupt':
        script += "os.write(2,b'not a completion proof');"
    elif mode == 'negative-exit':
        script += "os.kill(os.getpid(),signal.SIGTERM);"
    target['wire_script'] = script
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        with pytest.raises(PodgroveError, match='not retried'):
            execute(target, stdout=output, stderr=error)
    assert len(target['commands']) == 1 and 'ack' not in target['protocol']
    assert target['protocol'] == ['cleanup']
    assert all(child.poll() is not None for child in target.get('helpers', []))


@pytest.mark.parametrize('status', [0, 7, 143])
def test_empty_streams_and_application_exit_status_are_preserved(target, status):
    target['script'] = f'import sys;sys.exit({status})'
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        assert execute(target, stdin=subprocess.DEVNULL, stdout=output, stderr=error) == status
        assert output.tell() == error.tell() == 0
    assert target['protocol'] == ['ack']
    assert not Path('/tmp', 'podgrove-exec-' + target['nonce']).exists()


def test_non_tty_64mib_stdout_and_stderr_are_streamed_and_proved_independently(target):
    size = 64 * 1024 * 1024
    target['script'] = (
        'import os,threading,sys;block=bytes(range(256))*256;'
        'emit=lambda fd:[os.write(fd,block) for _ in range(1024)];'
        'worker=threading.Thread(target=emit,args=(2,));worker.start();'
        'emit(1);worker.join();sys.exit(7)'
    )
    expected = hashlib.sha256(bytes(range(256)) * 256 * 1024).hexdigest()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        assert execute(target, stdin=subprocess.DEVNULL, stdout=output, stderr=error) == 7
        for stream in (output, error):
            assert stream.tell() == size
            stream.seek(0)
            digest = hashlib.sha256()
            while block := stream.read(65536):
                digest.update(block)
            assert digest.hexdigest() == expected
    assert len(target['commands']) == 1 and target['protocol'] == ['ack']
    assert all(child.poll() is not None for child in target['helpers'])
    assert not Path('/tmp', 'podgrove-exec-' + target['nonce']).exists()


def test_unsupported_pipe_capture_is_rejected_before_discovery(target):
    with pytest.raises(PodgroveError, match='PIPE capture'):
        execute(target, stdout=subprocess.PIPE)
    assert not target['reads'] and not target['commands']


def test_broken_output_consumer_fails_without_ack_or_command_replay(target):
    target['script'] = 'import os;os.write(1,b"x"*1048576)'
    reader, writer = os.pipe()
    os.close(reader)
    try:
        assert os.get_blocking(writer)
        with pytest.raises(PodgroveError, match='not retried'):
            execute(target, stdin=subprocess.DEVNULL, stdout=writer, stderr=subprocess.DEVNULL)
        assert os.get_blocking(writer)
    finally:
        os.close(writer)
    assert len(target['commands']) == 1 and 'ack' not in target['protocol']
    assert all(child.poll() is not None for child in target['helpers'])


def test_blocked_stdout_does_not_block_stderr_or_ownership_cancellation(target, monkeypatch):
    target['script'] = ('import os,threading,time;'
                        'threading.Thread(target=lambda:os.write(1,b"x"*1048576)).start();'
                        'os.write(2,b"independent-stderr");time.sleep(30)')
    reader, writer = os.pipe()
    duplicate = os.dup(writer)
    original = transport._engine
    checked = []
    with tempfile.TemporaryFile() as error:
        def changed(*args, **kwargs):
            if kwargs.get('cancel') is not None:
                limit = time.monotonic() + 3
                while error.tell() < len(b'independent-stderr') and time.monotonic() < limit:
                    time.sleep(.01)
                checked.append(error.tell())
                target['resources']['pod']['metadata']['uid'] = 'replacement'
            return original(*args, **kwargs)
        monkeypatch.setattr(transport, '_engine', changed)
        started = time.monotonic()
        try:
            with pytest.raises(PodgroveError, match='not retried'):
                execute(target, stdin=subprocess.DEVNULL, stdout=writer, stderr=error, verification_interval=.05)
            assert os.get_blocking(writer) and os.get_blocking(duplicate)
        finally:
            for fd in (reader, writer, duplicate):
                os.close(fd)
        assert checked and checked[0] == len(b'independent-stderr')
        assert time.monotonic() - started < 5
    assert all(child.poll() is not None for child in target['helpers'])
    assert not Path('/tmp', 'podgrove-exec-' + target['nonce']).exists()


def test_exited_process_group_permission_race_does_not_mask_primary_failure(monkeypatch):
    process = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
    process.wait(timeout=3)
    def denied(pid, sig):
        assert pid == process.pid and sig == signal.SIGKILL
        raise PermissionError(1, 'Operation not permitted')
    monkeypatch.setattr(transport.os, 'killpg', denied)
    transport._stop(process)


@pytest.mark.parametrize('damage', ['missing-tail', 'wrong-hash', 'status-disagreement'])
def test_complete_footer_cannot_mask_lost_or_corrupted_output(target, monkeypatch, damage):
    from types import SimpleNamespace
    nonce = 'c' * 32
    monkeypatch.setattr(transport.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
    expected = b'complete-binary\0\xff'
    received = expected[:-3] if damage == 'missing-tail' else expected
    stdout_frame = frame(nonce, b'O', expected, 7)
    if damage == 'wrong-hash':
        stdout_frame = stdout_frame[:-65] + b'0' * 64 + b'\x1f'
    stderr_frame = frame(nonce, b'E', b'', 8 if damage == 'status-disagreement' else 7)
    target['wire_script'] = f'import os;os.write(1,{received + stdout_frame!r});os.write(2,{stderr_frame!r})'
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        with pytest.raises(PodgroveError, match='not retried'):
            execute(target, stdout=output, stderr=error)
        output.seek(0)
        assert b'PODGROVE-EXEC:' not in output.read()
    assert len(target['commands']) == 1 and 'ack' not in target['protocol']


def test_failed_ack_is_not_replayed_and_nonce_directory_is_removed(target, monkeypatch):
    original = transport._read
    attempts = []
    def refuse(command, **kwargs):
        if transport._ACK in command:
            attempts.append(command)
            raise PodgroveError('private transport diagnostic')
        return original(command, **kwargs)
    monkeypatch.setattr(transport, '_read', refuse)
    with pytest.raises(PodgroveError, match='not retried') as failure:
        execute(target, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert 'private' not in str(failure.value)
    assert len(attempts) == len(target['commands']) == 1
    assert not Path('/tmp', 'podgrove-exec-' + target['nonce']).exists()


def test_merged_output_sink_retains_bytes_without_changing_descriptor_flags(target):
    target['script'] = 'import os;os.write(1,b"stdout");os.write(2,b"stderr")'
    with tempfile.TemporaryFile() as output:
        duplicate = os.dup(output.fileno())
        try:
            assert execute(target, stdout=output, stderr=duplicate) == 0
            assert os.get_blocking(output.fileno()) and os.get_blocking(duplicate)
            output.seek(0)
            result = output.read()
            assert sorted(result.replace(b'stdout', b'O').replace(b'stderr', b'E')) == sorted(b'OE')
        finally:
            os.close(duplicate)


def test_native_tty_path_does_not_spawn_checksum_sinks(target):
    assert execute(target, tty=True, stdin=subprocess.DEVNULL) == 0
    assert not target.get('helpers') and not target['protocol']
    assert transport._STREAM_WRAPPER not in target['commands'][0][0]


def test_nonce_collision_never_removes_an_existing_directory(target, monkeypatch):
    from types import SimpleNamespace
    import uuid
    nonce = uuid.uuid4().hex
    directory = Path('/tmp', 'podgrove-exec-' + nonce)
    directory.mkdir(mode=0o700)
    (directory / 'owner').write_text('an-unrelated-owner\n')
    (directory / 'out').write_bytes(b'existing-data')
    monkeypatch.setattr(transport.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
    try:
        with pytest.raises(PodgroveError, match='not retried'):
            execute(target, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert (directory / 'out').read_bytes() == b'existing-data'
        assert (directory / 'owner').read_text() == 'an-unrelated-owner\n'
    finally:
        shutil.rmtree(directory)


def test_remote_missing_ack_has_bounded_wait_and_removes_its_directory(target):
    import uuid
    nonce = uuid.uuid4().hex
    wrapper = transport._STREAM_WRAPPER.replace('[ "$n" -lt 1200 ]', '[ "$n" -lt 2 ]')
    started = time.monotonic()
    result = subprocess.run(['sh', '-c', wrapper, 'fixture-stream', nonce, 'd' * 32,
                             sys.executable, '-c', 'pass'], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    assert result.returncode == 125 and time.monotonic() - started < 3
    assert result.stdout == frame(nonce, b'O', b'')
    assert result.stderr == frame(nonce, b'E', b'')
    assert not Path('/tmp', 'podgrove-exec-' + nonce).exists()


def test_stalled_consumer_can_resume_and_ack_waits_until_both_sinks_exit(target):
    target['script'] = 'import os;os.write(1,b"x"*1048576);os.write(2,b"independent")'
    reader, writer = os.pipe()
    received = bytearray()
    def consume():
        time.sleep(.2)
        while len(received) < 1048576:
            data = os.read(reader, 65536)
            if not data:
                break
            received.extend(data)
    consumer = threading.Thread(target=consume)
    consumer.start()
    try:
        with tempfile.TemporaryFile() as error:
            assert execute(target, stdout=writer, stderr=error) == 0
        assert received == b'x' * 1048576
        assert os.get_blocking(writer)
    finally:
        os.close(writer)
        consumer.join(timeout=3)
        os.close(reader)
    assert not consumer.is_alive() and target['protocol'] == ['ack']
    assert all(child.poll() == 0 for child in target['helpers'])
