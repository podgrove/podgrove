"""Owned direct exec: real local stdio, bounded reads and immutable target fences."""

import json
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
def target(monkeypatch):
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
    }

    def read(command, **_kwargs):
        state["reads"].append(command)
        if "get" in command:
            kind = command[command.index("get") + 1]
            return json.dumps(resources[kind]).encode()
        if "ps" in command:
            return "\n".join(state["ids"]).encode()
        assert "inspect" in command
        return (state["observed_id"] + "\n" + state["running"] + "\n" + json.dumps(state["labels"])).encode()

    original = subprocess.Popen

    def launch(command, **kwargs):
        state["commands"].append((command, kwargs))
        # Replace only kubectl's executable with an inert local child. All
        # production command construction, stdio, monitoring and cleanup run.
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


@pytest.mark.parametrize("tty", [False, True])
def test_cli_exec_uses_recorded_project_and_direct_transport(tmp_path, monkeypatch, tty):
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
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: tty)
    run_exec = Mock(return_value=7)
    monkeypatch.setattr(transport, "run_exec", run_exec)
    args = cli.parser().parse_args(["exec", "--context", "test-context", "--namespace", "team-dev",
                                    "--project-directory", str(root), "api", "--", "python", "-c", "literal argument"])
    assert cli.execute(args) == 7
    assert run_exec.call_args.args[1:] == (ident, "recorded-project", "api", ["python", "-c", "literal argument"])
    assert run_exec.call_args.kwargs == {"tty": tty}
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
