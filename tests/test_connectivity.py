"""Actual bounded HTTP reads and managed Compose overlay lifecycle."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from podgrove import connect, connectivity, reverse
from podgrove.compose import Compose
from podgrove.config import Config
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube

SOURCE, TARGET = "123456abcdef", "abcdef123456"
RULE = {"name": "database", "environment": TARGET, "service": "mongo", "port": 27017}


@contextmanager
def http_server(respond):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(.1)
    stopped, accepted = threading.Event(), threading.Event()
    errors = []
    def serve():
        wire = None
        try:
            while not stopped.is_set():
                try:
                    wire, _ = listener.accept()
                    break
                except socket.timeout:
                    continue
            if wire is None:
                return
            wire.settimeout(1)
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = wire.recv(4096)
                if not chunk:
                    return
                request += chunk
            assert request.startswith(b"GET /containers/json HTTP/1.1\r\n")
            accepted.set()
            respond(wire, stopped)
        except OSError:
            pass
        except BaseException as exc:
            errors.append(exc)
        finally:
            if wire is not None:
                wire.close()
    thread = threading.Thread(target=serve, name="connectivity-test-server", daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=listener.getsockname()[1], accepted=accepted)
    finally:
        stopped.set()
        thread.join(timeout=2)
        listener.close()
        assert not thread.is_alive()
        assert not errors


def no_read_watchers():
    return not any(thread.name == "podgrove-link-read" for thread in threading.enumerate())


@pytest.mark.parametrize("response", [
    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]",
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n2\r\n[]\r\n0\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n[]",
])
def test_actual_docker_read_accepts_complete_framed_and_close_delimited_json(response):
    with http_server(lambda wire, stopped: wire.sendall(response)) as tunnel:
        assert connect._docker_json(tunnel, "/containers/json", timeout=2) == []
    assert no_read_watchers()


@pytest.mark.parametrize("response", [
    b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n[]",
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\n[]",
    b"HTTP/1.1 503 Unavailable\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]",
    b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nnope",
])
def test_actual_docker_read_rejects_short_framing_http_errors_and_malformed_json(response):
    with http_server(lambda wire, stopped: wire.sendall(response)) as tunnel:
        with pytest.raises(PodgroveError, match="Cannot inspect"):
            connect._docker_json(tunnel, "/containers/json", timeout=2)
    assert no_read_watchers()


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_trickling_docker_response_has_total_deadline_and_reaps_watcher(phase):
    def respond(wire, stopped):
        if phase == "body":
            wire.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n[")
        else:
            wire.sendall(b"HTTP/1.1 200 OK\r\nX-Trickle: ")
        while not stopped.wait(.01):
            wire.sendall(b" ")
    with http_server(respond) as tunnel:
        start = time.monotonic()
        with pytest.raises(PodgroveError, match="Cannot inspect"):
            connect._docker_json(tunnel, "/containers/json", timeout=.2)
        assert time.monotonic() - start < 2
    assert no_read_watchers()


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_actual_pending_docker_read_cancels_without_waiting_for_idle_timeout(phase):
    def respond(wire, stopped):
        wire.sendall(b"HTTP/1.1 200 OK\r\n" if phase == "headers" else b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n[")
        stopped.wait(5)
    cancelled = threading.Event()
    failures = []
    with http_server(respond) as tunnel:
        def read():
            try:
                connect._docker_json(tunnel, "/containers/json", cancelled=cancelled, timeout=30)
            except BaseException as exc:
                failures.append(exc)
        reader = threading.Thread(target=read, name="connectivity-test-reader", daemon=True)
        reader.start()
        assert tunnel.accepted.wait(2)
        start = time.monotonic()
        cancelled.set()
        reader.join(timeout=2)
        assert not reader.is_alive()
        assert time.monotonic() - start < 2
        assert len(failures) == 1 and isinstance(failures[0], PodgroveError)
    assert no_read_watchers()


def test_pre_cancelled_docker_inspection_opens_no_socket(monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    opened = Mock(side_effect=AssertionError("cancelled inspection opened a socket"))
    monkeypatch.setattr(connect.http.client.HTTPConnection, "connect", opened)
    with pytest.raises(PodgroveError):
        connect._docker_json(SimpleNamespace(port=12345), "/containers/json", cancelled=cancelled)
    opened.assert_not_called()


def discovery(monkeypatch):
    kube = Kube("offline", "approved")
    def get(kind, name):
        assert kind in ("configmap", "statefulset") and name == "pg-" + TARGET
        return {"metadata": {"namespace": "approved", "name": name, "uid": "target-controller" if kind == "statefulset" else "lease",
                             "resourceVersion": "1", "labels": {MANAGED: "podgrove", ENVIRONMENT: TARGET}},
                "data": {"compose_project": "target-project"}}
    kube.get = Mock(side_effect=get)
    tunnel = Mock(port=12345)
    tunnel.identity_snapshot.return_value = {"expected": {"statefulset_uid": "target-controller", "pod_uid": "target-pod"}}
    monkeypatch.setattr(connect, "DockerTunnel", lambda *_: tunnel)
    labels = {"com.docker.compose.project": "target-project", "com.docker.compose.service": "mongo",
              "com.docker.compose.oneoff": "false"}
    cid = "a" * 64
    row = {"Id": cid, "State": "running", "Labels": deepcopy(labels)}
    detail = {"Id": cid, "State": {"Running": True}, "Config": {"Labels": deepcopy(labels)},
              "NetworkSettings": {"Ports": {"27017/tcp": [{"HostIp": "0.0.0.0", "HostPort": "32788"}]}}}
    return kube, tunnel, row, detail


@pytest.mark.parametrize("stage", ["list", "inspect"])
@pytest.mark.parametrize("label,value", [("project", "foreign-project"), ("service", "foreign-service"), ("oneoff", "true")])
def test_connect_refuses_spoofed_compose_identity_at_both_discovery_stages(monkeypatch, stage, label, value):
    kube, tunnel, row, detail = discovery(monkeypatch)
    labels = row["Labels"] if stage == "list" else detail["Config"]["Labels"]
    labels["com.docker.compose." + label] = value
    read = Mock(side_effect=[[row], detail])
    monkeypatch.setattr(connect, "_docker_json", read)
    with pytest.raises(PodgroveError, match="unambiguous|changed during discovery"):
        connect.discover_endpoint(kube, RULE)
    assert read.call_count == (1 if stage == "list" else 2)
    tunnel.refresh_identity.assert_not_called()
    tunnel.close.assert_called_once()


def test_connect_without_target_project_anchor_refuses_before_tunnel_open(monkeypatch):
    kube, tunnel, _, _ = discovery(monkeypatch)
    kube.get = Mock(return_value={"metadata": {"namespace": "approved", "name": "pg-" + TARGET, "uid": "lease",
        "resourceVersion": "1", "labels": {MANAGED: "podgrove", ENVIRONMENT: TARGET}}, "data": {}})
    with pytest.raises(PodgroveError, match="no recorded Compose project"):
        connect.discover_endpoint(kube, RULE)
    tunnel.start.assert_not_called()


def manager(tmp_path, monkeypatch, *, reverse_rules=True, connect_rules=True):
    config = Config(tmp_path, [tmp_path / "compose.yml"], connect=[RULE] if connect_rules else [],
                    reverse=[{"local_port": 8080, "remote_port": 8080, "local_host": "127.0.0.1"}] if reverse_rules else [])
    model = {"services": {"web": {"image": "busybox:1.37", "extra_hosts": {"unrelated": "192.0.2.8"}},
                          "worker": {"image": "busybox:1.37"}}}
    config.files[0].write_text(json.dumps(model))
    compose = Compose(config)
    links = Mock(aliases={"database.podgrove": "10.0.0.8"})
    links.snapshot.return_value = {"state": "ready"}
    backward = Mock()
    backward.snapshot.return_value = {"state": "ready"}
    monkeypatch.setattr(connectivity, "EnvironmentLinks", lambda *_: links)
    monkeypatch.setattr(reverse, "ReverseForward", lambda *_: backward)
    coordinator = connectivity.Connectivity(Kube("offline", "approved"), SOURCE, compose, model, ("controller", "pod"))
    return coordinator, compose, model, links, backward


def test_connectivity_overlay_is_private_preserves_inputs_and_closes_both_components(tmp_path, monkeypatch):
    coordinator, compose, model, links, backward = manager(tmp_path, monkeypatch)
    before = deepcopy(model)
    coordinator.start()
    overlay = coordinator.overlay
    assert overlay is not None and overlay.stat().st_mode & 0o777 == 0o600
    assert compose.overlay_files == [overlay]
    assert json.loads(overlay.read_text()) == {"services": {name: {"extra_hosts": {
        "database.podgrove": "10.0.0.8", "host.docker.internal": "host-gateway"}} for name in model["services"]}}
    assert model == before
    assert compose.command("up")[-3:] == ["--file", str(overlay), "up"]
    assert coordinator.snapshot()["state"] == "ready"
    coordinator.close()
    links.close.assert_called_once()
    backward.close.assert_called_once()
    assert compose.overlay_files == [] and not overlay.exists()


def test_partial_connectivity_start_failure_reaps_started_components(tmp_path, monkeypatch):
    coordinator, compose, _, links, backward = manager(tmp_path, monkeypatch)
    backward.start.side_effect = PodgroveError("listener occupied")
    with pytest.raises(PodgroveError, match="listener occupied"):
        coordinator.start()
    links.close.assert_called_once()
    backward.close.assert_called_once()
    assert coordinator.overlay is None and compose.overlay_files == []


def test_connectivity_close_still_removes_overlay_after_component_failure(tmp_path, monkeypatch):
    coordinator, compose, _, links, backward = manager(tmp_path, monkeypatch)
    coordinator.start()
    overlay = coordinator.overlay
    backward.close.side_effect = PodgroveError("close failed")
    with pytest.raises(PodgroveError, match="close failed"):
        coordinator.close()
    links.close.assert_called_once()
    assert not overlay.exists() and compose.overlay_files == []


def test_real_compose_merge_keeps_unrelated_extra_hosts_and_managed_routes(tmp_path, monkeypatch):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI unavailable")
    version = subprocess.run([docker, "compose", "version"], capture_output=True, timeout=10)
    if version.returncode:
        pytest.skip("Docker Compose plugin unavailable")
    coordinator, compose, _, _, _ = manager(tmp_path, monkeypatch)
    coordinator.start()
    try:
        result = subprocess.run(compose.command("config", "--format", "json"), cwd=tmp_path,
                                env={**os.environ, "DOCKER_HOST": "tcp://127.0.0.1:1", "KUBECONFIG": os.devnull},
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        merged = json.loads(result.stdout)
        hosts = merged["services"]["web"]["extra_hosts"]
        if isinstance(hosts, list):
            hosts = dict(re.split(r"[=:]", entry, maxsplit=1) for entry in hosts)
        for alias, value in {"unrelated": "192.0.2.8", "database.podgrove": "10.0.0.8", "host.docker.internal": "host-gateway"}.items():
            assert hosts[alias] in (value, [value])
    finally:
        coordinator.close()


def test_oversized_docker_response_is_refused_with_bounded_read():
    payload = b"[" + b" " * (4 * 1024 * 1024) + b"]"
    response = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload
    with http_server(lambda wire, stopped: wire.sendall(response)) as tunnel:
        with pytest.raises(PodgroveError, match="Cannot inspect"):
            connect._docker_json(tunnel, "/containers/json", timeout=2)
    assert no_read_watchers()


def test_disabled_connectivity_creates_no_overlay_or_components(tmp_path, monkeypatch):
    coordinator, compose, _, links, backward = manager(tmp_path, monkeypatch, reverse_rules=False, connect_rules=False)
    coordinator.start()
    assert coordinator.overlay is None and compose.overlay_files == []
    assert coordinator.snapshot()["state"] == "disabled"
    coordinator.close()
    links.start.assert_not_called()
    backward.start.assert_not_called()


@pytest.mark.parametrize("cancelled_by", ["component", "caller"])
def test_kube_adapter_preserves_both_component_and_docker_tunnel_cancellation(cancelled_by):
    outer, inner = threading.Event(), threading.Event()
    kube = Kube("offline", "approved")
    def call(*args, **kwargs):
        combined = kwargs["cancel_event"]
        assert not combined.is_set()
        (outer if cancelled_by == "component" else inner).set()
        assert combined.is_set()
        return SimpleNamespace(stdout="", returncode=0)
    kube.call = Mock(side_effect=call)
    connect._CancellableKube(kube, outer).call("get", "pod", "owned", cancel_event=inner)
    kube.call.assert_called_once()
# Startup cancellation must stop setup without becoming a live-session timer.
@pytest.mark.parametrize("cause", ["deadline", "cancel"])
def test_connectivity_startup_budget_cancels_pending_link_before_later_phases(tmp_path, monkeypatch, cause):
    import threading
    import time
    from podgrove.config import Config
    from podgrove.compose import Compose
    from podgrove.connectivity import Connectivity
    from podgrove import connectivity
    stopped = threading.Event()
    cancel = threading.Event()
    link = Mock(aliases={})
    link.start.side_effect = lambda: stopped.wait(2)
    link.cancel.side_effect = stopped.set
    monkeypatch.setattr(connectivity, "EnvironmentLinks", Mock(return_value=link))
    config = Config(tmp_path, [], connect=[{"name": "api", "environment": "b" * 12, "service": "gateway", "port": 8080}])
    compose = Compose(config)
    value = Connectivity(Mock(), "a" * 12, compose, {"services": {"web": {}}}, {})
    timer = threading.Timer(.05, cancel.set) if cause == "cancel" else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(PodgroveError, match="deadline expired"):
            value.start(deadline=started + (.05 if cause == "deadline" else 5), cancel_event=cancel)
    finally:
        if timer:
            timer.join()
    assert stopped.is_set() and time.monotonic() - started < 1
    assert compose.overlay_files == []
    link.close.assert_called_once()
    assert not any(thread.name == "podgrove-connect-start" for thread in threading.enumerate())


def test_successful_setup_disarms_its_deadline_without_stopping_live_links(tmp_path, monkeypatch):
    import time
    from podgrove.config import Config
    from podgrove.compose import Compose
    from podgrove.connectivity import Connectivity
    from podgrove import connectivity
    link = Mock(aliases={"api.podgrove": "10.1.2.3"})
    monkeypatch.setattr(connectivity, "EnvironmentLinks", Mock(return_value=link))
    config = Config(tmp_path, [], connect=[{"name": "api", "environment": "b" * 12, "service": "gateway", "port": 8080}])
    compose = Compose(config)
    value = Connectivity(Mock(), "a" * 12, compose, {"services": {"web": {}}}, {})
    try:
        value.start(deadline=time.monotonic() + .1)
        time.sleep(.15)
        link.cancel.assert_not_called()
        assert len(compose.overlay_files) == 1
    finally:
        value.close()
