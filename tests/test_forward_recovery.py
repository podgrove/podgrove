"""Independent local children/sockets exercise idle application-forward recovery."""
from copy import deepcopy
import json
import socket
import sys
import time
from types import SimpleNamespace

import pytest

from podgrove.errors import PodgroveError
from podgrove.forward import ForwardOwnershipError, Tunnel, free_port
from podgrove.kube import ENVIRONMENT, MANAGED, REQUEST_PROCESS_TIMEOUT

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

    def call(self, *args, timeout):
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
    def make(*, modes=("normal",), retry_delays=(.02, .03, .04), timeout=2, verification_interval=30):
        kube = LocalKube(tmp_path / (str(len(made)) + ".control"), modes)
        port = free_port()
        item = Tunnel(kube, IDENT, [(port, 8080)], poll_interval=.03, retry_delays=retry_delays,
                      verification_interval=verification_interval)
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
