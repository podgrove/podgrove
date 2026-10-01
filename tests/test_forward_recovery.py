"""Independent local children/sockets exercise idle application-forward recovery."""
from copy import deepcopy
import errno
import io
import json
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from podgrove.errors import PodgroveError
from podgrove.forward import ForwardOwnershipError, Tunnel, free_port
from podgrove.kube import ENVIRONMENT, MANAGED, REQUEST_PROCESS_TIMEOUT
from podgrove.process import run

IDENT = "012345abcdef"
# A real separate process stands in for kubectl, with real TCP listeners. It
# receives only this test's ports/control pathname, never kube credentials.
SERVER = r'''
import json, pathlib, selectors, socket, sys, time
ports, control, generation, mode = json.loads(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4]
if mode == 'fail': raise SystemExit('simulated forwarding failure')
if mode == 'hang':
    while True: time.sleep(.05)
selector = selectors.DefaultSelector()
listeners = []
for port in ports:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', port)); sock.listen(); sock.setblocking(False)
    listeners.append(sock); selector.register(sock, selectors.EVENT_READ, True)
    print(f'Forwarding from 127.0.0.1:{port} -> 8080', flush=True)
while True:
    if control.exists() and control.read_text() == generation:
        for sock in listeners: selector.unregister(sock); sock.close()
        listeners = []
    for key, _ in selector.select(.02):
        if key.data:
            client, _ = key.fileobj.accept(); client.setblocking(False)
            selector.register(client, selectors.EVENT_READ, False)
        else:
            message = key.fileobj.recv(65536)
            if message: key.fileobj.sendall(message)
            else: selector.unregister(key.fileobj); key.fileobj.close()
'''


class LocalKube:
    namespace = "offline-owned"

    def __init__(self, path, modes=("normal",)):
        self.path, self.modes, self.commands, self.reads = path, modes, [], []
        labels = {MANAGED: "podgrove", ENVIRONMENT: IDENT}
        self.controller = {"metadata": {"name": "pg-" + IDENT, "namespace": self.namespace,
                                       "uid": "original-controller", "labels": dict(labels)}}
        self.pod = {"metadata": {"name": "pg-" + IDENT + "-0", "namespace": self.namespace,
                                "uid": "original-pod", "labels": dict(labels), "ownerReferences": [{
                                    "apiVersion": "apps/v1", "kind": "StatefulSet", "name": "pg-" + IDENT,
                                    "uid": "original-controller", "controller": True}]}}

    def call(self, *args, timeout, cancel_event=None):
        assert args[:1] == ("get",) and timeout == REQUEST_PROCESS_TIMEOUT
        self.reads.append(args)
        return SimpleNamespace(stdout=json.dumps(self.controller if args[1] == "statefulset" else self.pod))

    def command(self, *args):
        assert args[0] == "port-forward" and args[1] == "pod/pg-" + IDENT + "-0"
        self.commands.append(args)
        generation = len(self.commands)
        ports = [int(arg.split(":")[0]) for arg in args if ":" in arg and arg[0].isdigit()]
        mode = self.modes[min(generation - 1, len(self.modes) - 1)]
        return [sys.executable, "-u", "-c", SERVER, json.dumps(ports), str(self.path), str(generation), mode]


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), "Recovery condition did not arrive within the bounded deadline"


def echo(port, content=b"application still reachable"):
    with socket.create_connection(("127.0.0.1", port), timeout=.5) as client:
        client.sendall(content)
        assert client.recv(len(content)) == content


@pytest.fixture
def tunnel(tmp_path):
    made = []
    # Real child startup can exceed 300ms under aggregate-suite load; recovery
    # assertions have their own bounded waits, and timeout-specific tests override this.
    def make(*, modes=("normal",), retry_delays=(.02, .03, .04), timeout=2, verification_interval=30,
             max_verification_age=120):
        kube = LocalKube(tmp_path / (str(len(made)) + ".control"), modes)
        port = free_port()
        item = Tunnel(kube, IDENT, [(port, 8080)], poll_interval=.03, retry_delays=retry_delays,
                      verification_interval=verification_interval, max_verification_age=max_verification_age)
        made.append(item)
        item.start(timeout=timeout)
        return item, kube, port
    yield make
    for item in made:
        item.close()
        assert not item._thread or not item._thread.is_alive()
        assert item.process is None or item.process.poll() is not None
        assert item.log is None


def test_idle_child_exit_recovers_same_port_with_ownership_checks(tunnel):
    item, kube, port = tunnel()
    events = []
    item.on_change = lambda status: events.append(deepcopy(status))
    first = item.process
    echo(port)
    first.terminate()
    first.wait(timeout=2)
    wait_until(lambda: item.process is not first and item.snapshot()["state"] == "ready")
    echo(port)
    assert first.poll() is not None and item.ports == [(port, 8080)]
    assert len(kube.commands) == 2 and len(kube.reads) >= 8
    assert any(event["state"] == "reconnecting" for event in events)
    assert events[-1]["state"] == "ready" and events[-1]["attempts"] == 1
    assert events[-1]["checked_at"] >= events[0]["checked_at"]


@pytest.mark.parametrize("malformed", [False, True])
def test_api_read_outage_keeps_existing_forward_then_verifies_same_uid(tunnel, malformed):
    item, kube, port = tunnel(verification_interval=.03)
    original = kube.call
    child = item.process
    available = threading.Event()
    def read(*args, **kwargs):
        if not available.is_set():
            if malformed:
                return SimpleNamespace(stdout="incomplete JSON")
            raise PodgroveError("kubectl timed out after 35s")
        return original(*args, **kwargs)
    kube.call = read
    wait_until(lambda: item.snapshot()["state"] == "reconnecting")
    assert item.process is child and child.poll() is None
    echo(port)
    available.set()
    wait_until(lambda: item.snapshot()["state"] == "ready")
    assert item.process is child and len(kube.commands) == 1
    echo(port)


def test_expired_forward_ownership_proof_closes_listener_and_bounds_retries(tunnel):
    item, kube, port = tunnel(verification_interval=.03, max_verification_age=.2)
    child = item.process
    def unavailable(*args, **kwargs):
        raise PodgroveError("kubectl connection reset by peer")
    kube.call = unavailable
    wait_until(lambda: item.snapshot()["state"] == "disconnected")
    assert child.poll() is not None and len(kube.commands) == 1
    assert item.snapshot()["attempts"] == 3
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=.1)


def test_slow_verification_cannot_extend_existing_forward_proof_deadline(tunnel, tmp_path):
    item, kube, port = tunnel(verification_interval=.03, max_verification_age=.4, retry_delays=())
    child = item.process
    entered = tmp_path / "entered"
    deadlines = []
    def blocked(*args, timeout, cancel_event=None):
        deadlines.append(timeout)
        return run([sys.executable, "-c", "import pathlib,sys,time;pathlib.Path(sys.argv[1]).touch();time.sleep(60)",
                    str(entered)], timeout=timeout, cancel_event=cancel_event)
    kube.call = blocked
    wait_until(entered.exists)
    echo(port)
    verified = item._verified_at
    wait_until(lambda: item.snapshot()["state"] == "disconnected", timeout=2)
    assert time.monotonic() - verified < 1.5
    assert child.poll() is not None and len(kube.commands) == 1
    assert len(deadlines) == 1 and 0 < deadlines[0] < .4


def test_live_child_without_listener_is_restarted_without_source_edits(tunnel):
    item, kube, port = tunnel()
    first = item.process
    kube.path.write_text("1")
    wait_until(lambda: item.process is not first and item.snapshot()["state"] == "ready")
    assert first.poll() is not None
    echo(port)


def test_failed_restarts_are_bounded_and_report_disconnected(tunnel):
    item, kube, _ = tunnel(modes=("normal", "fail"))
    item.process.terminate()
    item.process.wait(timeout=2)
    wait_until(lambda: item.snapshot()["state"] == "disconnected")
    assert len(kube.commands) == 4
    assert item.snapshot()["attempts"] == 3 and "exhausted" in item.snapshot()["error"]
    time.sleep(.1)
    assert len(kube.commands) == 4 and item.check()["state"] == "disconnected"


@pytest.mark.parametrize("change", ["pod-uid", "controller-uid", "foreign", "owner-reference", "terminating"])
def test_recovery_never_attaches_to_changed_engine(tunnel, change):
    item, kube, port = tunnel()
    if change == "pod-uid":
        kube.pod["metadata"]["uid"] = "replacement"
    elif change == "controller-uid":
        kube.controller["metadata"]["uid"] = "replacement"
    elif change == "foreign":
        kube.pod["metadata"]["labels"][MANAGED] = "foreign"
    elif change == "owner-reference":
        kube.pod["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    else:
        kube.pod["metadata"]["deletionTimestamp"] = "2026-09-26T00:00:00Z"
    item.process.terminate()
    item.process.wait(timeout=2)
    wait_until(lambda: item.snapshot()["state"] == "disconnected")
    assert len(kube.commands) == 1
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=.1)


def test_periodic_ownership_loss_closes_listener_without_waiting_for_process_exit(tunnel):
    item, kube, port = tunnel(verification_interval=.04)
    child = item.process
    kube.pod["metadata"]["labels"][ENVIRONMENT] = "foreign"
    wait_until(lambda: item.snapshot()["state"] == "disconnected" and child.poll() is not None)
    assert len(kube.commands) == 1
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=.1)


def test_shutdown_interrupts_restart_wait_and_reaps_owned_child(tunnel):
    item, kube, _ = tunnel(modes=("normal", "hang"), timeout=30)
    item.process.terminate()
    item.process.wait(timeout=2)
    wait_until(lambda: len(kube.commands) == 2)
    child = item.process
    start = time.monotonic()
    item.close()
    assert time.monotonic() - start < 1
    assert child.poll() is not None and not item._thread.is_alive()
    assert item.snapshot()["state"] == "disconnected"


def test_occupied_original_port_is_not_silently_replaced_or_adopted(tunnel):
    item, kube, port = tunnel(retry_delays=(.3, .02))
    child = item.process
    child.terminate()
    child.wait(timeout=2)
    with socket.socket() as foreign:
        foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        foreign.bind(("127.0.0.1", port))
        foreign.listen()
        wait_until(lambda: item.snapshot()["state"] == "disconnected")
        assert item.ports == [(port, 8080)] and len(kube.commands) == 3
        assert item.process.poll() is not None


def test_initial_foreign_engine_is_refused_before_starting_child(tmp_path):
    kube = LocalKube(tmp_path / "control")
    kube.pod["metadata"]["labels"][MANAGED] = "foreign"
    item = Tunnel(kube, IDENT, [(free_port(), 8080)])
    with pytest.raises(ForwardOwnershipError):
        item.start(timeout=.1)
    assert kube.commands == [] and item.process is None and item.log is None


def test_process_exit_during_post_launch_ownership_check_never_reports_ready(tmp_path):
    kube = LocalKube(tmp_path / "control")
    item = Tunnel(kube, IDENT, [(free_port(), 8080)])
    original = kube.call
    def read(*args, **kwargs):
        if len(kube.reads) == 2:
            item.process.terminate()
            item.process.wait(timeout=2)
        return original(*args, **kwargs)
    kube.call = read
    with pytest.raises(PodgroveError, match="stopped while verifying"):
        item.start(timeout=1)
    assert item.snapshot()["state"] == "disconnected"
    assert item.process.poll() is not None and item.log is None and item._thread is None


def test_shutdown_cancels_slow_api_read_and_closes_listener(tunnel, tmp_path):
    item, kube, port = tunnel(verification_interval=.03)
    original = kube.call
    started = tmp_path / "slow-api-pid"
    def slow_read(*args, timeout, cancel_event=None):
        run([sys.executable, "-c", "import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)",
             str(started)], timeout=timeout, cancel_event=cancel_event)
        return original(*args, timeout=timeout)
    kube.call = slow_read
    wait_until(started.exists)
    echo(port)
    start = time.monotonic()
    item.close()
    assert time.monotonic() - start < 2
    assert not item._thread.is_alive()
    assert item.process.poll() is not None
    assert item.snapshot()["state"] == "disconnected"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=.1)


def test_cancellable_command_preserves_input_output_and_exit():
    payload = "message\n" * 100000
    result = run([sys.executable, "-c", "import sys,time;time.sleep(.2);sys.stdout.write(sys.stdin.read());sys.stderr.write('detail');sys.exit(7)"],
                 input=payload, timeout=3, cancel_event=threading.Event(), check=False)
    assert result.stdout == payload and result.stderr == "detail" and result.returncode == 7


def test_cancellable_command_still_enforces_deadline():
    with pytest.raises(PodgroveError, match="timed out after 0.2s"):
        run([sys.executable, "-c", "import time;time.sleep(60)"], timeout=.2, cancel_event=threading.Event())


def test_cancelled_command_never_launches(tmp_path):
    cancelled = threading.Event()
    cancelled.set()
    marker = tmp_path / "must-not-exist"
    with pytest.raises(PodgroveError, match="cancelled"):
        run([sys.executable, "-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()", str(marker)],
            cancel_event=cancelled)
    assert not marker.exists()


@pytest.mark.parametrize("outcome", ["killed", "missing"])
def test_cancellation_and_finally_signal_owned_group_once_even_when_second_call_would_be_eperm(monkeypatch, outcome):
    from podgrove import process as commands
    import signal
    entered, cleanup_waiting, release = threading.Event(), threading.Event(), threading.Event()
    cancelled = threading.Event()
    calls, waited = [], []
    caller = threading.get_ident()
    child = SimpleNamespace(pid=24680, returncode=None, stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())

    class TerminationLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if threading.get_ident() == caller:
                cleanup_waiting.set()
            self.lock.acquire()

        def __exit__(self, *_):
            self.lock.release()

    def communicate(*, input, timeout):
        cancelled.set()
        assert entered.wait(2), "Cancellation monitor never attempted group termination"
        return "", ""

    def killpg(pid, sig):
        calls.append((pid, sig))
        if len(calls) > 1:
            raise PermissionError(errno.EPERM, "Exited Darwin process group cannot be signalled twice")
        assert threading.current_thread().name == "podgrove-command-cancel"
        child.returncode = -signal.SIGKILL if outcome == "killed" else 0
        entered.set()
        assert release.wait(3), "Cleanup did not contend with the cancellation monitor"
        if outcome == "missing":
            raise ProcessLookupError(errno.ESRCH, "Owned group already gone")

    child.communicate = communicate
    child.wait = lambda *, timeout: waited.append(timeout) or child.returncode
    monkeypatch.setattr(commands.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(commands, "os", SimpleNamespace(killpg=killpg))
    monkeypatch.setattr(commands, "threading", SimpleNamespace(Event=threading.Event, Thread=threading.Thread, Lock=TerminationLock))

    def allow_signal_to_finish():
        cleanup_waiting.wait(2)
        release.set()

    coordinator = threading.Thread(target=allow_signal_to_finish)
    coordinator.start()
    try:
        with pytest.raises(PodgroveError, match="fixture cancelled"):
            commands.run(["fixture"], timeout=5, cancel_event=cancelled)
        assert cleanup_waiting.is_set()
        assert calls == [(child.pid, signal.SIGKILL)]
        assert waited == [2]
        assert all(pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))
        assert not any(thread.name == "podgrove-command-cancel" for thread in threading.enumerate())
    finally:
        release.set()
        coordinator.join(3)
        for thread in threading.enumerate():
            if thread.name == "podgrove-command-cancel":
                thread.join(3)
        assert not coordinator.is_alive()


@pytest.mark.parametrize("where", ["monitor", "finally"])
@pytest.mark.parametrize("still_live", [False, True])
def test_first_group_permission_error_is_reported_and_cleanup_still_waits_and_closes(monkeypatch, where, still_live):
    from podgrove import process as commands
    import subprocess
    attempted, cancelled = threading.Event(), threading.Event()
    calls, waited = [], []
    denial = PermissionError(errno.EPERM, "First group termination denied")
    child = SimpleNamespace(pid=24680, returncode=None if still_live else 0,
                            stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())

    def communicate(*, input, timeout):
        if where == "monitor":
            cancelled.set()
            assert attempted.wait(2)
        return "", ""

    def killpg(pid, sig):
        calls.append((pid, sig))
        attempted.set()
        if len(calls) == 1:
            raise denial

    def wait(*, timeout):
        waited.append(timeout)
        if still_live:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return child.returncode

    child.communicate, child.wait = communicate, wait
    monkeypatch.setattr(commands.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(commands, "os", SimpleNamespace(killpg=killpg))
    with pytest.raises(PermissionError) as error:
        commands.run(["fixture"], timeout=5, cancel_event=cancelled)
    assert error.value is denial
    assert len(calls) == 1 and waited == [2]
    assert all(pipe.closed for pipe in (child.stdin, child.stdout, child.stderr))
    assert not any(thread.name == "podgrove-command-cancel" for thread in threading.enumerate())


def test_cancellation_kills_owned_helpers_after_process_group_leader_exits(tmp_path, monkeypatch):
    import os
    import signal
    import subprocess
    marker = tmp_path / "helper-pid"
    original = subprocess.Popen
    signal_group = os.killpg
    signals = []
    leaders = []
    cancelled = threading.Event()

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        leaders.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)

    def killpg(pid, sig):
        assert leaders and pid == leaders[0].pid and sig == signal.SIGKILL
        signals.append((pid, sig))
        return signal_group(pid, sig)

    monkeypatch.setattr(os, "killpg", killpg)

    def cancel_after_leader_exit():
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if marker.exists() and leaders and leaders[0].poll() is not None:
                cancelled.set()
                return
            time.sleep(.01)
        cancelled.set()

    watcher = threading.Thread(target=cancel_after_leader_exit)
    watcher.start()
    started = time.monotonic()
    helper_inactive = False
    try:
        with pytest.raises(PodgroveError, match="cancelled"):
            run([sys.executable, "-c",
                 "import os,pathlib,sys,time;pid=os.fork();"
                 "os._exit(0) if pid else None;"
                 "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)", str(marker)],
                timeout=5, cancel_event=cancelled)
        assert time.monotonic() - started < 2
        assert leaders[0].returncode == 0
        assert signals == [(leaders[0].pid, signal.SIGKILL)]
        assert not any(thread.name == "podgrove-command-cancel" for thread in threading.enumerate())
        helper = int(marker.read_text())
        # A terminated orphan may briefly remain a zombie until the OS reaps it.
        status = original(["ps", "-o", "stat=", "-p", str(helper)], stdout=subprocess.PIPE, text=True)
        assert status.communicate(timeout=2)[0].strip() in ("", "Z", "Z+")
        helper_inactive = True
    finally:
        watcher.join(timeout=3)
        if not helper_inactive and marker.exists():
            try:
                os.kill(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
