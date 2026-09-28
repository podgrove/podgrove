"""Dashboard reads are bounded, authenticated, scoped, and do not extend TTL."""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from podgrove import cli, state, web
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, engine_pod_name


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "worktree"
    root.mkdir()
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "private-state"))
    ident = state.identity(root)
    data = {"identity": ident, "root": str(root), "context": "test-context", "namespace": "default",
            "status": "ready", "node_mode": "shared", "last_activity": 123.0, "created_at": 100.0,
            "ttl_seconds": 600, "token": "a" * 32, "docker_host": "tcp://127.0.0.1:4444",
            "socket": str(Path(tempfile.gettempdir()) / f"podgrove-{os.getuid()}-{'a' * 16}.sock"),
            "error": "secret-supervisor-error", "config_path": "/private/config.env",
            "services": [{"Service": "api", "Name": "fixture-api-1", "State": "running", "Health": "healthy",
                          "Image": "python:3.12-alpine", "Command": "secret-command", "Env": ["secret-env"]}],
            "ports": [{"service": "api", "target": 8080, "local": 12345, "url": "https://untrusted.invalid"}]}
    state.write(state.state_path(root, data["context"]), data)
    return root, data, web.Dashboard(data["context"])


def test_listing_is_sanitized_and_does_not_write_state_or_ping_lifecycle(environment, monkeypatch):
    root, data, backend = environment
    path = state.state_path(root, data["context"])
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    monkeypatch.setattr(web, "control", Mock(side_effect=AssertionError("No list control request")))
    monkeypatch.setattr(state, "write", Mock(side_effect=AssertionError("No state writes")))
    monkeypatch.setattr(web, "bounded_read_command", Mock(side_effect=AssertionError("No list cluster request")))
    result = backend.environments()
    row = result["environments"][0]
    assert row["identity"] == data["identity"] and row["last_activity"] == 123
    assert row["source"] == "local_snapshot" and row["health_fresh"] is False and row["health_observed_at"] is None
    assert row["counts"] == {"total": 1, "running": 1, "healthy": 1, "unhealthy": 0, "exited": 0}
    assert row["ports"][0]["url"] == "http://127.0.0.1:12345"
    encoded = json.dumps(result)
    for hidden in ("secret-", "docker_host", "4444", "/private/config.env", data["token"], data["socket"]):
        assert hidden not in encoded
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_listing_without_state_never_creates_the_state_directory(tmp_path, monkeypatch):
    home = tmp_path / "absent"
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(home))
    assert web.Dashboard("explicit-context").environments()["environments"] == []
    assert not home.exists()


def test_sync_recovery_summary_projects_only_sanitized_bounded_diagnostics(environment):
    root, data, backend = environment
    data["sync_status"] = {"state": "reconnecting", "attempts": 2, "next_retry_at": 123.5,
                           "checked_at": 120.5, "error": "Bearer private-credential token=another-secret " + "x" * 3000,
                           "docker_host": "private-host", "helper_id": "private-helper", "token": "private-token"}
    state.write(state.state_path(root, data["context"]), data)
    result = backend.environments()["environments"][0]["sync_status"]
    assert result["state"] == "reconnecting" and result["attempts"] == 2
    assert result["next_retry_at"] == 123.5 and result["checked_at"] == 120.5
    assert set(result) == {"state", "attempts", "next_retry_at", "checked_at", "error"}
    assert "private-" not in json.dumps(result) and "another-secret" not in result["error"]
    assert "[REDACTED]" in result["error"] and len(result["error"]) == 1024


@pytest.mark.parametrize("invalid", [None, [], "invalid", {"state": "private-secret", "attempts": True,
                                                         "checked_at": float("nan"), "next_retry_at": -1}])
def test_sync_recovery_summary_rejects_malformed_metadata(invalid):
    result = web._sync_status({"sync_status": invalid})
    assert result == {"state": "unknown", "attempts": 0, "checked_at": None, "next_retry_at": None, "error": ""}


@pytest.mark.parametrize("quote", ['"', "'"])
def test_sync_recovery_redacts_quoted_credentials_before_clipping(quote):
    error = "password=" + quote + "private-multiword " * 300 + quote + " safe diagnostic"
    result = web._sync_status({"sync_status": {"state": "disconnected", "error": error}})
    assert result["error"] == "password=[REDACTED] safe diagnostic"
    assert "private-multiword" not in json.dumps(result)


@pytest.mark.parametrize("current", ["reconnecting", "disconnected", "ready"])
def test_sync_recovery_detail_uses_control_state_without_writing_or_hiding_engine(engine, monkeypatch, current):
    data, backend, *_ = engine
    before = state.state_path(Path(data["root"]), data["context"]).read_bytes()
    monkeypatch.setattr(web, "control", Mock(return_value={
        "ok": True, "status": "ready" if current == "ready" else "degraded",
        "sync_status": {"state": current, "attempts": 1, "error": "token=hidden-credential"},
        "forward_status": {"state": "ready"}}))
    result = backend.detail(data["identity"])
    assert result["sync_status"]["state"] == current
    assert result["sync_status"]["error"] == "token=[REDACTED]"
    assert result["forward_status"]["state"] == "ready"
    assert result["engine"]["pod"]["ready"] is True
    assert result["status"] == ("ready" if current == "ready" else "degraded")
    assert state.state_path(Path(data["root"]), data["context"]).read_bytes() == before


def test_listing_filters_other_context_and_namespace(environment):
    _, data, _ = environment
    assert web.Dashboard("other-context").environments()["environments"] == []
    assert web.Dashboard(data["context"], "podgrove-testing").environments()["environments"] == []


@pytest.fixture
def engine(environment, monkeypatch):
    _, data, backend = environment
    ident = data["identity"]
    def metadata(name, uid):
        return {"name": name, "namespace": "default", "uid": uid,
                "labels": {MANAGED: "podgrove", ENVIRONMENT: ident}}
    controller = {"metadata": metadata("pg-" + ident, "controller-uid"), "spec": {"replicas": 1},
                  "status": {"readyReplicas": 1}}
    pod = {"metadata": metadata(engine_pod_name(ident), "pod-uid"),
           "spec": {"nodeName": "test-node", "volumes": [{"persistentVolumeClaim": {"claimName": "pg-" + ident}}],
                    "containers": [{"name": "docker", "env": [{"name": "PRIVATE_SECRET", "value": "hidden-env"}],
                                    "resources": {"requests": {"cpu": "250m", "memory": "2Gi"},
                                                  "limits": {"cpu": "2", "memory": "2Gi"}}}]},
           "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                      "containerStatuses": [{"restartCount": 2}]}}
    pod["metadata"]["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": "StatefulSet", "name": "pg-" + ident,
                                            "uid": "controller-uid", "controller": True}]
    pvc = {"metadata": metadata("pg-" + ident, "pvc-uid"),
           "spec": {"resources": {"requests": {"storage": "5Gi"}}, "storageClassName": "gp3", "volumeName": "pv-owned"},
           "status": {"phase": "Bound", "capacity": {"storage": "5Gi"}}}
    objects = {"statefulset": controller, "pod": pod, "persistentvolumeclaim": pvc}
    reads = []
    def read(kube, kind, name):
        reads.append((kube.context, kube.namespace, kind, name))
        return objects[kind]
    monkeypatch.setattr(backend, "_kube_read", read)
    rows = [{"Id": "b" * 64, "Names": ["/fixture-api-1"], "State": "running", "Status": "Up 10 seconds (healthy)",
             "Image": "python:3.12-alpine", "Command": "hidden-command", "Labels": {
                 "com.docker.compose.project": "fixture", "com.docker.compose.service": "api", "private": "hidden-label"}}]
    monkeypatch.setattr(backend, "_containers", lambda _data: rows)
    return data, backend, objects, reads, rows


def test_detail_reads_only_owned_resources_and_public_allocation_fields(engine):
    data, backend, _, reads, _ = engine
    result = backend.detail(data["identity"])
    assert result["health_fresh"] is True and result["source"] == "live"
    assert result["engine"]["pod"]["ready"] and result["engine"]["pod"]["restarts"] == 2
    assert result["engine"]["pod"]["resources"]["requests"]["memory"] == "2Gi"
    assert result["storage"]["capacity"] == "5Gi" and result["storage"]["volume"] == "pv-owned"
    assert {row[2] for row in reads} == {"statefulset", "pod", "persistentvolumeclaim"}
    assert all(row[:2] == (data["context"], "default") for row in reads)
    encoded = json.dumps(result)
    for hidden in ("hidden-env", "hidden-command", "hidden-label", "Config.Env", data["token"]):
        assert hidden not in encoded


def test_detail_exposes_observed_ephemeral_and_initializer_allocations(engine):
    data, backend, objects, _, _ = engine
    spec = objects["pod"]["spec"]
    spec["containers"][0]["resources"]["requests"]["ephemeral-storage"] = "1Gi"
    spec["containers"][0]["resources"]["limits"]["ephemeral-storage"] = "8Gi"
    spec["initContainers"] = [{"name": "storage", "resources": {
        "requests": {"cpu": "0", "memory": "32Mi"}, "limits": {"memory": "64Mi"}}}]
    result = backend.detail(data["identity"])["engine"]
    assert result["pod"]["resources"]["requests"]["ephemeral-storage"] == "1Gi"
    assert result["pod"]["resources"]["limits"]["ephemeral-storage"] == "8Gi"
    assert result["init_containers"] == spec["initContainers"]


@pytest.mark.parametrize("case", ["controller_labels", "pod_owner", "pvc_labels", "claim", "namespace", "deleting"])
def test_foreign_engine_metadata_never_reaches_docker_or_log_reads(engine, monkeypatch, case):
    data, backend, objects, _, _ = engine
    if case == "controller_labels":
        objects["statefulset"]["metadata"]["labels"][ENVIRONMENT] = "other"
    elif case == "pod_owner":
        objects["pod"]["metadata"]["ownerReferences"][0]["uid"] = "other"
    elif case == "pvc_labels":
        objects["persistentvolumeclaim"]["metadata"]["labels"][MANAGED] = "other"
    elif case == "claim":
        objects["pod"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "other"
    elif case == "namespace":
        objects["pod"]["metadata"]["namespace"] = "other"
    else:
        objects["pod"]["metadata"]["deletionTimestamp"] = "now"
    monkeypatch.setattr(backend, "_containers", Mock(side_effect=AssertionError("No unowned Docker access")))
    monkeypatch.setattr(web, "bounded_read_command", Mock(side_effect=AssertionError("No unowned logs")))
    result = backend.detail(data["identity"])
    assert result["warnings"] and result["health_fresh"] is False
    with pytest.raises(web.WebError):
        backend.logs(data["identity"], source="engine", service=None, tail=10)


def test_selected_service_logs_use_only_validated_container_ids_and_redact_common_credentials(engine, monkeypatch):
    data, backend, _, _, _ = engine
    observed = []
    message = b'normal line\nAuthorization: Bearer private-value\n{"password":"secret-pass"}\n'
    raw = b"\x01\0\0\0" + len(message).to_bytes(4, "big") + message
    def docker(_data, path, **kwargs):
        observed.append((path, kwargs))
        return raw, False
    monkeypatch.setattr(backend, "_docker", docker)
    result = backend.logs(data["identity"], source="service", service="api", tail=20)
    assert result["source"] == "service" and result["service"] == "api"
    assert "normal line" in result["text"] and "private-value" not in result["text"] and "secret-pass" not in result["text"]
    assert observed[0][0].startswith("/containers/" + "b" * 64 + "/logs?")
    assert "tail=20" in observed[0][0] and observed[0][1]["limit"] == web.MAX_LOG
    with pytest.raises(web.WebError, match="Unknown Compose"):
        backend.logs(data["identity"], source="service", service="other", tail=20)


def test_engine_logs_are_bounded_and_scoped_to_owned_engine_container(engine, monkeypatch):
    data, backend, _, _, _ = engine
    command = Mock(return_value=b"engine started\n")
    monkeypatch.setattr(web, "bounded_read_command", command)
    result = backend.logs(data["identity"], source="engine", service=None, tail=100)
    args = command.call_args.args[0]
    assert args[:5] == ["kubectl", "--context", data["context"], "--namespace", "default"]
    assert "logs" in args and "pod/" + engine_pod_name(data["identity"]) in args
    assert args[args.index("--container") + 1] == "docker" and "--limit-bytes" in args and "--follow" not in args
    assert result["text"] == "engine started\n"


@pytest.mark.parametrize("source,service,tail", [("exec", "api", 10), ("service", "../api", 10),
                                                ("service", "--all", 10), ("engine", "docker", 10),
                                                ("engine", None, 0), ("engine", None, 201)])
def test_log_selection_cannot_be_used_as_a_command_or_unbounded_read(engine, monkeypatch, source, service, tail):
    data, backend, _, _, _ = engine
    monkeypatch.setattr(backend, "_engine", Mock(side_effect=AssertionError("No invalid request reads")))
    with pytest.raises(web.WebError) as exc:
        backend.logs(data["identity"], source=source, service=service, tail=tail)
    assert exc.value.status == 400


def test_docker_reads_ping_without_touch_and_never_export_connection_credentials(environment, monkeypatch):
    _, data, _ = environment
    monkeypatch.setattr(Path, "lstat", lambda _self: SimpleNamespace(st_uid=os.getuid(), st_mode=stat.S_IFSOCK | 0o600))
    control = Mock(return_value={"ok": True})
    monkeypatch.setattr(web, "control", control)
    response = Mock(status=200, length=None)
    response.read1.side_effect = [b"[]", b""]
    connection = Mock()
    connection.getresponse.return_value = response
    http = Mock(return_value=connection)
    monkeypatch.setattr(web.http.client, "HTTPConnection", http)
    assert web.Dashboard._docker(data, "/containers/json") == (b"[]", False)
    assert control.call_args.args[1] == "ping"
    assert connection.request.call_args.args == ("GET", "/containers/json")
    connection.close.assert_called_once()
    bad = {**data, "docker_host": "tcp://remote.invalid:2375"}
    with pytest.raises(web.WebError):
        web.Dashboard._docker(bad, "/containers/json")
    assert http.call_count == 1


@pytest.mark.parametrize("framing,body,declared,limit,allow_truncated,error", [
    ("length", b"complete", 8, 16, False, None),
    ("length", b"", 0, 16, False, None),
    ("length", b"partial", 100, 128, True, "declared length"),
    ("length", b"x" * 16, 16, 16, False, None),
    ("length", b"x" * 17, 17, 16, True, None),
    ("length", b"x" * 17, 17, 16, False, "size limit"),
    ("length", b"x" * 20, 100, 16, True, None),
    ("chunked", b"\x00\xffcomplete", None, 32, False, None),
    ("chunked", b"", None, 32, False, None),
    ("chunked", b"complete", 999, 32, False, None),
    ("incomplete-chunked", b"partial", None, 32, True, "unavailable"),
    ("close-delimited", b"complete", None, 32, False, None),
])
def test_docker_snapshot_http_completion_and_explicit_size_limit(
        monkeypatch, framing, body, declared, limit, allow_truncated, error):
    """Real HTTPResponse EOF/length semantics, including a socket closed by read1."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            if "chunked" in framing:
                self.send_header("Transfer-Encoding", "chunked")
            if declared is not None:
                self.send_header("Content-Length", str(declared))
            self.send_header("Connection", "close")
            self.end_headers()
            wire = body
            if "chunked" in framing:
                wire = (f"{len(body):x}\r\n".encode() + body + b"\r\n") if body else b""
                if framing != "incomplete-chunked":
                    wire += b"0\r\n\r\n"
            try:
                self.wfile.write(wire)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # A caller enforcing its size limit may close early.
            self.close_connection = True

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=upstream.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()

    def connect(_data, *, timeout):
        connection = http.client.HTTPConnection("127.0.0.1", upstream.server_port, timeout=timeout)
        connection.connect()
        return connection

    monkeypatch.setattr(web.Dashboard, "_docker_connection", staticmethod(connect))
    path = "/containers/" + "a" * 64 + "/logs?stdout=1&stderr=1&tail=100"
    try:
        if error:
            with pytest.raises(web.WebError, match=error):
                web.Dashboard._docker({}, path, limit=limit, allow_truncated=allow_truncated, timeout=1)
        else:
            result = web.Dashboard._docker({}, path, limit=limit, allow_truncated=allow_truncated, timeout=1)
            assert result == (body[:limit], len(body) > limit)
        assert requests == [path]  # This correctness repair does not replay reads.
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize("trickle", ["headers", "body"])
def test_docker_absolute_deadline_terminates_real_trickle_http(environment, monkeypatch, trickle):
    _, data, _ = environment
    monkeypatch.setattr(Path, "lstat", lambda _self: SimpleNamespace(st_uid=os.getuid(), st_mode=stat.S_IFSOCK | 0o600))
    monkeypatch.setattr(web, "control", Mock(return_value={"ok": True}))
    stopped = threading.Event()
    class SlowHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            try:
                if trickle == "body":
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    value = b"x"
                else:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    self.wfile.flush()
                    value = b"a"
                for _ in range(100):
                    if stopped.wait(0.03):
                        return
                    self.wfile.write(value)
                    self.wfile.flush()
            except OSError:
                pass
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=upstream.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    data["docker_host"] = f"tcp://127.0.0.1:{upstream.server_port}"
    started = time.monotonic()
    try:
        with pytest.raises(web.WebError):
            web.Dashboard._docker(data, "/containers/json", timeout=0.2)
        assert time.monotonic() - started < 0.8
    finally:
        stopped.set()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)


def test_engine_log_uid_change_is_rejected_before_returning_buffered_logs(engine, monkeypatch):
    data, backend, objects, _, _ = engine
    def log_then_replace(*_args, **_kwargs):
        objects["pod"]["metadata"]["uid"] = "replacement-uid"
        return b"must not be returned"
    monkeypatch.setattr(web, "bounded_read_command", log_then_replace)
    with pytest.raises(web.WebError, match="changed during"):
        backend.logs(data["identity"], source="engine", service=None, tail=10)


def test_docker_response_limit_and_container_schema_fail_closed(environment, monkeypatch):
    _, data, backend = environment
    monkeypatch.setattr(backend, "_docker", lambda *_args, **_kwargs: (b'[{"Id":"bad"}]', False))
    with pytest.raises(web.WebError, match="invalid Compose"):
        backend._containers(data)


@pytest.fixture
def server(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<!doctype html><title>Dashboard</title>")
    backend = Mock()
    backend.environments.return_value = {"context": "test-context", "environments": [], "errors": []}
    backend.detail.return_value = {"identity": "123456abcdef", "warnings": []}
    backend.logs.return_value = {"text": "logs", "truncated": False}
    instance = web.DashboardServer("test-context", backend=backend, static_dir=assets)
    thread = threading.Thread(target=instance.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield instance, backend
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def request(server, path="/api/environments", *, headers=None, method="GET", body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    result = response.status, dict(response.getheaders()), payload
    connection.close()
    return result


def test_http_api_requires_unforgeable_header_and_keeps_token_out_of_response(server):
    instance, backend = server
    assert instance.server_address[0] == "127.0.0.1" and "#token=" in instance.url
    assert request(instance)[0] == 403
    assert request(instance, headers={"X-Podgrove-Token": "wrong"})[0] == 403
    code, headers, body = request(instance, headers={"X-Podgrove-Token": instance.token})
    assert code == 200 and json.loads(body)["environments"] == []
    assert instance.token.encode() not in body
    assert headers["Cache-Control"] == "no-store" and headers["X-Frame-Options"] == "DENY"
    backend.environments.assert_called_once()


@pytest.mark.parametrize("extra", [{"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"},
                                   {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}])
def test_dns_rebinding_and_cross_site_requests_are_denied_even_with_token(server, extra):
    instance, backend = server
    assert request(instance, headers={"X-Podgrove-Token": instance.token, **extra})[0] == 403
    backend.environments.assert_not_called()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_http_mutation_methods_are_never_dispatched(server, method):
    instance, backend = server
    assert request(instance, headers={"X-Podgrove-Token": instance.token}, method=method)[0] == 405
    assert not backend.mock_calls


@pytest.mark.parametrize("path", ["/api/environments/../../secrets", "/api/environments/123456abcdef?exec=rm",
                                  "/api/environments/123456abcdef/logs?tail=1&tail=2",
                                  "/api/environments/123456abcdef/logs?command=exec", "/api/environments?token=bad"])
def test_http_unknown_paths_and_query_controls_are_rejected(server, path):
    instance, backend = server
    assert request(instance, path, headers={"X-Podgrove-Token": instance.token})[0] in (400, 404)
    assert not backend.mock_calls


def test_http_errors_are_json_and_static_paths_cannot_read_arbitrary_files(server):
    instance, backend = server
    backend.detail.side_effect = web.WebError("Owned engine unavailable", 503)
    code, _, body = request(instance, "/api/environments/123456abcdef", headers={"X-Podgrove-Token": instance.token})
    assert code == 503 and json.loads(body) == {"error": "Owned engine unavailable"}
    assert request(instance, "/")[0] == 200
    assert request(instance, "/../web.py", headers={"X-Podgrove-Token": instance.token})[0] == 404
    for _ in range(4):
        instance.request_slots.acquire()
    try:
        assert request(instance, headers={"X-Podgrove-Token": instance.token})[0] == 429
    finally:
        for _ in range(4):
            instance.request_slots.release()


def test_slow_connections_have_a_global_thread_cap(server):
    instance, _ = server
    clients = []
    try:
        for _ in range(8):
            clients.append(socket.create_connection(instance.server_address, timeout=1))
        deadline = time.monotonic() + 1
        while instance.connection_slots._value and time.monotonic() < deadline:
            time.sleep(0.01)
        assert instance.connection_slots._value == 0
        extra = socket.create_connection(instance.server_address, timeout=1)
        extra.settimeout(1)
        assert extra.recv(1) == b""
        extra.close()
    finally:
        for client in clients:
            client.close()


def test_bounded_subprocess_timeout_size_limit_and_stderr_do_not_expose_private_values():
    assert web.bounded_read_command([sys.executable, "-c", "print('read-only')"]) == b"read-only\n"
    with pytest.raises(web.WebError, match="size limit"):
        web.bounded_read_command([sys.executable, "-c", "print('x'*20000)"], limit=100)
    with pytest.raises(web.WebError, match="timed out"):
        web.bounded_read_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)
    with pytest.raises(web.WebError) as exc:
        web.bounded_read_command([sys.executable, "-c", "import sys; sys.stderr.write('private-value'); sys.exit(1)"])
    assert "private-value" not in str(exc.value)


def test_exited_child_group_permission_race_preserves_size_limit_and_closes_pipes(monkeypatch):
    original_popen = subprocess.Popen
    processes = []
    def start(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process
    def exited_group(pid, sig):
        process, = processes
        assert pid == process.pid and sig == signal.SIGKILL
        process.wait(timeout=2)  # Deterministically race group cleanup against exit.
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(web.subprocess, "Popen", start)
    monkeypatch.setattr(web.os, "killpg", exited_group)
    with pytest.raises(web.WebError, match="size limit") as failure:
        web.bounded_read_command([sys.executable, "-c", "print('x'*1000)"], limit=10)
    process, = processes
    assert process.poll() == 0
    assert process.stdout.closed and process.stderr.closed
    assert not getattr(failure.value, "__notes__", [])


@pytest.mark.parametrize("leader_exits", [False, True])
def test_cleanup_kills_private_group_helpers_even_after_leader_exit(leader_exits):
    child = "import sys,time;print('helper-ready',flush=True);time.sleep(60)"
    program = ("import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1]]);"
               + ("raise SystemExit(0)" if leader_exits else "time.sleep(60)"))
    process = subprocess.Popen([sys.executable, "-c", program, child], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(2), "Fixture helper did not start"
            assert os.read(process.stdout.fileno(), 128) == b"helper-ready\n"
            if leader_exits:
                assert process.wait(timeout=2) == 0
            else:
                assert process.poll() is None
            web._stop(process)
            assert process.poll() == (0 if leader_exits else -signal.SIGKILL)
            assert ready.select(2), "Orphan helper retained the capture pipe after cancellation"
            assert os.read(process.stdout.fileno(), 128) == b""
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def test_live_group_permission_denial_is_not_mistaken_for_success(monkeypatch):
    process = Mock(pid=12345)
    process.poll.return_value = None
    monkeypatch.setattr(web.os, "killpg", Mock(side_effect=PermissionError(1, "Operation not permitted")))
    with pytest.raises(PermissionError):
        web._stop(process)
    process.wait.assert_not_called()


def test_live_cleanup_denial_preserves_primary_web_error_and_reports_incomplete_cleanup(monkeypatch):
    original_popen, original_killpg = subprocess.Popen, os.killpg
    processes = []
    def start(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(web.subprocess, "Popen", start)
    monkeypatch.setattr(web.os, "killpg", Mock(side_effect=PermissionError(1, "private cleanup diagnostic")))
    try:
        with pytest.raises(web.WebError, match="size limit") as failure:
            web.bounded_read_command([sys.executable, "-c", "import time;print('x'*1000,flush=True);time.sleep(60)"], limit=10)
        process, = processes
        assert process.poll() is None
        assert process.stdout.closed and process.stderr.closed
        assert failure.value.__notes__ == ["Dashboard command cleanup could not be confirmed"]
        assert "private" not in str(failure.value)
    finally:
        for process in processes:
            original_killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_cli_web_dispatches_before_worktree_state_or_lifecycle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(state, "state_path", Mock(side_effect=AssertionError("No environment state writes")))
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    args = cli.parser().parse_args(["web", "--context", "test-context", "--namespace", "team-development", "--port", "4321", "--no-open"])
    assert cli.execute(args) == 0
    serve.assert_called_once_with("test-context", port=4321, namespace="team-development", open_browser=False)
    with pytest.raises(PodgroveError, match="context"):
        web.Dashboard(None)


def test_cli_web_launch_uses_custom_namespace_from_file_without_compose_or_env_reads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PODGROVE_CONTEXT", raising=False)
    (tmp_path / "podgrove.yml").write_text("""cluster:
  context: portable-context
  namespace: team-development
  storage_class: team-ssd
compose:
  files: [missing-compose.yml]
  env_file: missing-private.env
""")
    monkeypatch.setattr(state, "state_path", Mock(side_effect=AssertionError("No environment state writes")))
    serve = Mock(return_value=0)
    monkeypatch.setattr(web, "serve", serve)
    args = cli.parser().parse_args(["web", "--no-open"])
    assert cli.execute(args) == 0
    serve.assert_called_once_with("portable-context", port=0, namespace="team-development", open_browser=False)


@pytest.mark.parametrize("mode", ["shared", "worktree", "exclusive", None])
def test_dashboard_reports_saved_namespace_mode_without_inferring_or_mutating(environment, mode):
    root, data, _ = environment
    data["namespace"] = "wt-explicit-team"
    if mode is not None:
        data["namespace_mode"] = mode
    else:
        data.pop("namespace_mode", None)
    before = dict(data)
    assert web.Dashboard._summary(data)["namespace_mode"] == mode
    assert data == before


@pytest.mark.parametrize("configured,expected", [
    ({"blocked_cidrs": ["44.55.0.0/16", "2001:DB8::/32"]},
     {"blocked_cidrs": ["44.55.0.0/16", "2001:db8::/32"]}),
    ({}, {"blocked_cidrs": []}),
    (None, None),
    ({"blocked_cidrs": ["private-invalid-value"]}, None),
    ({"blocked_cidrs": ["44.55.0.1/16"]}, None),
    ({"blocked_cidrs": [], "credential": "private-secret-value"}, None),
    ({"blocked_cidrs": ["44.55.0.0/16"] * 129}, None),
])
def test_saved_network_projection_is_validated_without_inferring_or_mutating(environment, configured, expected):
    _, data, _ = environment
    if configured is not None:
        data["network"] = configured
    before = json.dumps(data)
    projected = web.Dashboard._summary(data)
    assert projected["network"] == expected
    assert "private-" not in json.dumps(projected)
    assert json.dumps(data) == before


@pytest.mark.parametrize("context", ["   ", "bad\ncontext", "bad\x7fcontext", "x" * 513])
def test_dashboard_rejects_invalid_context_before_local_or_cluster_reads(context, monkeypatch):
    monkeypatch.setattr(web, "Kube", Mock(side_effect=AssertionError("No cluster access")))
    monkeypatch.setattr(state, "local_records", Mock(side_effect=AssertionError("No state reads")))
    with pytest.raises(PodgroveError, match="context"):
        web.Dashboard(context)


@pytest.mark.parametrize("failure", [OSError("opener unavailable"), web.webbrowser.Error("no browser"), False])
def test_browser_launch_failure_keeps_printed_dashboard_url_serving(monkeypatch, capsys, failure):
    server = Mock(url="http://127.0.0.1:54321/#token=private-test-token")
    monkeypatch.setattr(web, "DashboardServer", Mock(return_value=server))
    opener = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    monkeypatch.setattr(web.webbrowser, "open", opener)
    assert web.serve("test-context") == 0
    server.serve_forever.assert_called_once_with(poll_interval=0.2)
    server.server_close.assert_called_once()
    output = capsys.readouterr()
    assert output.out.strip() == server.url
    assert "use the URL printed above" in output.err
    assert "private-test-token" not in output.err


def test_interrupt_during_browser_launch_closes_real_bound_socket(monkeypatch, capsys):
    actual_server = web.DashboardServer
    created = []
    def create(*args, **kwargs):
        server = actual_server(*args, **kwargs)
        created.append(server)
        return server
    monkeypatch.setattr(web, "DashboardServer", create)
    monkeypatch.setattr(web.webbrowser, "open", Mock(side_effect=KeyboardInterrupt()))
    assert web.serve("test-context") == 0
    assert created[0].socket.fileno() == -1
    with socket.socket() as retry:
        retry.bind(created[0].server_address)
    assert created[0].url in capsys.readouterr().out


def test_occupied_dashboard_port_fails_without_opening_browser_or_closing_existing_listener(monkeypatch):
    opener = Mock()
    monkeypatch.setattr(web.webbrowser, "open", opener)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(PodgroveError, match="Cannot bind dashboard"):
            web.serve("test-context", port=listener.getsockname()[1])
        assert listener.fileno() >= 0
    opener.assert_not_called()


def test_no_open_serves_without_attempting_browser_and_closes_on_interrupt(monkeypatch):
    server = Mock(url="http://127.0.0.1:54321/#token=test")
    server.serve_forever.side_effect = KeyboardInterrupt()
    monkeypatch.setattr(web, "DashboardServer", Mock(return_value=server))
    opener = Mock()
    monkeypatch.setattr(web.webbrowser, "open", opener)
    assert web.serve("test-context", open_browser=False) == 0
    opener.assert_not_called()
    server.server_close.assert_called_once()


def test_configuration_metadata_is_current_allowlisted_settings_without_referenced_file_reads(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    selected = root / "settings" / "development.yml"
    selected.parent.mkdir()
    selected.write_text("""version: 1
cluster: {context: configured-context, namespace: team-development, namespace_mode: worktree, storage_class: development-ssd}
network: {blocked_cidrs: [44.55.0.0/16, '2001:DB8:ABCD::/48']}
size: small
ttl: 45m
node_mode: tainted
tainted_nodes:
  selector: {pool: development}
  taint: {key: isolation, value: development, effect: NoExecute}
compose:
  files: [compose.yml, overrides/dev.yml]
  profiles: [extra]
  project_directory: backend
  env_file: private.env
forward:
  - {service: api, port: 8080}
  - {service: admin, port: 9090, local: 49152}
""")
    (root / "private.env").write_text("API_TOKEN=private-environment-value\n")
    (root / ".env").write_text("SECRET=private-default-env\n")
    before = (selected.read_bytes(), selected.stat().st_mtime_ns)
    real_open = os.open
    opened = []
    def guarded_open(name, *args, **kwargs):
        opened.append(str(name))
        assert str(name) not in ("private.env", ".env", "compose.yml", "dev.yml", "backend")
        return real_open(name, *args, **kwargs)
    monkeypatch.setattr(web.os, "open", guarded_open)
    monkeypatch.setattr(web, "bounded_read_command", Mock(side_effect=AssertionError("No CLI execution")))
    monkeypatch.setattr(state, "write", Mock(side_effect=AssertionError("No state mutation")))
    data = {"root": str(root), "config_path": str(selected), "node_mode": "shared", "ttl_seconds": 1,
            "context": "running-context", "namespace": "default"}
    result = web.configuration_metadata(data)
    assert result == {"source": "current_file", "status": "available", "file": "settings/development.yml",
                          "warning": None, "settings": {
                              "version": 1, "sync": {"exclude": []}, "size": "small", "node_mode": "tainted", "ttl_seconds": 2700,
                              "resources_mode": "preset", "storage": {"size": "20Gi"},
                              "resources": {"requests": {"cpu": "250m", "memory": "2Gi"},
                                            "limits": {"cpu": "2", "memory": "2Gi", "ephemeral-storage": "4Gi"}},
                              "init_resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                                 "limits": {"cpu": "100m", "memory": "32Mi"}},
                          "network": {"blocked_cidrs": ["44.55.0.0/16", "2001:db8:abcd::/48"]},
                          "cluster": {"context": "configured-context", "namespace": "team-development", "namespace_mode": "worktree", "storage_class": "development-ssd"},
                          "tainted_nodes": {"selector": {"pool": "development"}, "taint": {
                              "key": "isolation", "value": "development", "effect": "NoExecute"}},
                          "compose": {"files": ["compose.yml", "overrides/dev.yml"], "profiles": ["extra"],
                                      "project_directory": "backend"},
                          "forward": [{"service": "api", "port": 8080}, {"service": "admin", "port": 9090, "local": 49152}]}}
    assert data["node_mode"] == "shared" and data["ttl_seconds"] == 1
    assert data["context"] == "running-context" and data["namespace"] == "default"
    assert "development.yml" in opened and "private-env" not in json.dumps(result)
    assert "env_file" not in json.dumps(result)
    assert (selected.read_bytes(), selected.stat().st_mtime_ns) == before


def test_default_configuration_metadata_and_missing_file_have_distinct_meaning(tmp_path):
    root = tmp_path.resolve()
    data = {"root": str(root), "config_path": None}
    missing = web.configuration_metadata(data)
    assert missing["status"] == "missing" and missing["settings"] is None
    assert missing["file"] == "podgrove.yml" and missing["warning"]
    (root / "podgrove.yml").write_text("{}\n")
    result = web.configuration_metadata(data)
    assert result["status"] == "available" and result["warning"] is None
    assert result["settings"] == {"version": 1, "sync": {"exclude": []}, "size": "medium", "node_mode": "shared", "ttl_seconds": 28800,
                                  "resources_mode": "preset", "storage": {"size": "20Gi"},
                                  "resources": {"requests": {"cpu": "1", "memory": "8Gi"},
                                                "limits": {"cpu": "4", "memory": "8Gi", "ephemeral-storage": "4Gi"}},
                                  "init_resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                                     "limits": {"cpu": "100m", "memory": "32Mi"}},
                                  "network": {"blocked_cidrs": []},
                                  "cluster": {"context": None, "namespace": None, "namespace_mode": "shared", "storage_class": None},
                                  "tainted_nodes": None,
                                  "compose": {"files": None, "profiles": [], "project_directory": "."}, "forward": None}
    (root / "podgrove.yml").write_text("forward: []\n")
    assert web.configuration_metadata(data)["settings"]["forward"] == []


@pytest.mark.parametrize("cluster,expected", [
    ("{context: configured-context}", {"context": "configured-context", "namespace": None, "namespace_mode": "shared", "storage_class": None}),
    ("{namespace: team-development}", {"context": None, "namespace": "team-development", "namespace_mode": "shared", "storage_class": None}),
])
def test_configuration_cluster_projection_does_not_infer_running_or_environment_values(tmp_path, monkeypatch, cluster, expected):
    root = tmp_path.resolve()
    (root / "podgrove.yml").write_text("cluster: " + cluster + "\n")
    monkeypatch.setenv("PODGROVE_CONTEXT", "private-environment-value")
    monkeypatch.setenv("KUBECONFIG", "/private/kubeconfig")
    data = {"root": str(root), "context": "running-context", "namespace": "running-namespace"}
    result = web.configuration_metadata(data)
    assert result["status"] == "available"
    assert result["settings"]["cluster"] == expected
    assert "private" not in json.dumps(result)
    assert "running-" not in json.dumps(result)


@pytest.mark.parametrize("network", [
    {"blocked_cidrs": ["private-secret-value"]},
    {"blocked_cidrs": ["44.55.0.1/16"]},
    {"blocked_cidrs": ["2001:DB8::/32", "2001:db8::/32"]},
    {"blocked_cidrs": ["44.55.0.0/16"] * 129},
    {"blocked_cidrs": [], "credentials": "private-secret-value"},
])
def test_invalid_current_network_is_unavailable_without_exposing_input(tmp_path, network):
    root = tmp_path.resolve()
    path = root / "podgrove.yml"
    path.write_text(json.dumps({"network": network}))
    before = path.read_bytes()
    result = web.configuration_metadata({"root": str(root)})
    assert result["status"] == "unavailable" and result["settings"] is None
    assert "private-" not in json.dumps(result)
    assert path.read_bytes() == before


def test_configuration_metadata_respects_custom_file_and_does_not_fall_back(tmp_path):
    root = tmp_path.resolve()
    (root / "podgrove.yml").write_text("size: large\n")
    data = {"root": str(root), "config_path": "missing.yml"}
    assert web.configuration_metadata(data)["status"] == "missing"
    assert web.configuration_metadata(data)["settings"] is None
    data["config_path"] = str(root.parent / "external-private.yml")
    result = web.configuration_metadata(data)
    assert result["status"] == "unavailable" and result["file"] is None
    assert "external-private" not in json.dumps(result)


@pytest.mark.parametrize("case", ["file_symlink", "directory_symlink", "root_symlink", "ancestor_symlink", "hardlink", "fifo", "directory", "missing_root"])
def test_configuration_metadata_refuses_special_or_replaced_paths_before_read(tmp_path, monkeypatch, case):
    parent = tmp_path.resolve()
    root = parent / "worktree"
    root.mkdir()
    selected = root / "podgrove.yml"
    outside = parent / "sensitive.yml"
    outside.write_text("size: large\n")
    data = {"root": str(root), "config_path": None}
    if case == "file_symlink":
        selected.symlink_to(outside)
    elif case == "directory_symlink":
        (root / "linked").symlink_to(parent, target_is_directory=True)
        data["config_path"] = "linked/sensitive.yml"
    elif case == "root_symlink":
        link = parent / "root-link"
        link.symlink_to(root, target_is_directory=True)
        data["root"] = str(link)
    elif case == "ancestor_symlink":
        link = parent / "parent-link"
        link.symlink_to(parent, target_is_directory=True)
        data["root"] = str(link / "worktree")
    elif case == "hardlink":
        os.link(outside, selected)
    elif case == "fifo":
        os.mkfifo(selected)
    elif case == "directory":
        selected.mkdir()
    else:
        root.rmdir()
    monkeypatch.setattr(web.os, "read", Mock(side_effect=AssertionError("Unsafe file must never be read")))
    started = time.monotonic()
    result = web.configuration_metadata(data)
    assert time.monotonic() - started < 1
    assert result["status"] == "unavailable" and result["settings"] is None
    assert "sensitive" not in result["warning"]


@pytest.mark.parametrize("untrusted", ["file", "root"])
def test_configuration_metadata_refuses_another_users_file_or_root(tmp_path, monkeypatch, untrusted):
    root = tmp_path.resolve()
    (root / "podgrove.yml").write_text("size: small\n")
    actual_fstat = os.fstat
    def altered(descriptor):
        info = actual_fstat(descriptor)
        if (untrusted == "file" and stat.S_ISREG(info.st_mode)) or (untrusted == "root" and stat.S_ISDIR(info.st_mode)):
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            fields["st_uid"] = os.getuid() + 1
            return SimpleNamespace(**fields)
        return info
    monkeypatch.setattr(web.os, "fstat", altered)
    monkeypatch.setattr(web.os, "read", Mock(side_effect=AssertionError("Untrusted file must never be read")))
    assert web.configuration_metadata({"root": str(root)})["status"] == "unavailable"


@pytest.mark.parametrize("body", [
    b"unknown: private-secret-value\n",
    b"size: small\nsize: large # private-secret-value\n",
    b"compose: {files: [private-secret-value\n",
    b"!!python/object/apply:os.system ['echo private-secret-value']\n",
    b"compose: &reference {profiles: [extra]}\nforward: *reference\n",
    b"compose: &reference {profiles: [*reference]}\n",
    ("compose: " + "[" * 30 + "private-secret-value" + "]" * 30).encode(),
    ("compose:\n  profiles: [" + ",".join("p"+str(index) for index in range(2200)) + "]\n").encode(),
    b"#" * (web.MAX_CONFIG + 1),
    b"\xff\xfeprivate-secret-value",
    b"ttl: 9007199254740993s\n",
    b"forward: [{service: api, port: 8080}, {service: api, port: 8080}]\n",
    b"forward: [{service: api, port: 8080, local: 12345}, {service: api, port: 9090, local: 12345}]\n",
    b"compose: {files: [../private-secret-value]}\n",
    b"tainted_nodes: {selector: {invalid/key/label: yes}}\n",
    b"cluster: {context: '   '}\n",
    b"cluster: {namespace: '../private-secret-value'}\n",
    b"cluster: {context: local, token: private-secret-value}\n",
    b"cluster: {context: local, kubeconfig: /private-secret-value}\n",
])
def test_configuration_parse_and_semantic_failures_are_bounded_and_redacted(tmp_path, body):
    root = tmp_path.resolve()
    (root / "podgrove.yml").write_bytes(body)
    started = time.monotonic()
    result = web.configuration_metadata({"root": str(root)})
    assert time.monotonic() - started < 1
    assert result["status"] == "unavailable" and result["settings"] is None
    assert "private-secret-value" not in json.dumps(result)
    assert "traceback" not in json.dumps(result).lower()


def test_configuration_changed_during_read_is_not_reported_as_a_valid_snapshot(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    selected = root / "podgrove.yml"
    selected.write_text("size: small\n")
    original_read = os.read
    changed = False
    def edit_after_read(descriptor, size):
        nonlocal changed
        value = original_read(descriptor, size)
        if not changed:
            changed = True
            selected.write_text("size: large\n")
        return value
    monkeypatch.setattr(web.os, "read", edit_after_read)
    assert web.configuration_metadata({"root": str(root)})["status"] == "unavailable"


def test_config_detail_keeps_current_file_separate_from_saved_runtime_and_never_modifies_state(environment, engine):
    root, data, backend = environment
    (root / "podgrove.yml").write_text("size: large\nnode_mode: tainted\nttl: 3h\n")
    data["config_path"] = None
    path = state.state_path(root, data["context"])
    state.write(path, data)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    result = backend.detail(data["identity"])
    assert result["node_mode"] == "shared" and result["ttl_seconds"] == 600
    assert result["configuration"]["settings"]["node_mode"] == "tainted"
    assert result["configuration"]["settings"]["ttl_seconds"] == 10800
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_large_saved_endpoint_and_service_lists_expose_omission_counts(environment):
    _, data, backend = environment
    data["ports"] = [{"service": "api", "local": 10000+index, "target": 8080} for index in range(260)]
    data["services"] = [{"Service": f"api{index}", "State": "running"} for index in range(258)]
    result = backend._summary(data)
    assert len(result["ports"]) == 256 and result["ports_truncated"] is True and result["ports_omitted"] == 4
    assert len(result["services"]) == 256 and result["services_truncated"] is True
    assert result["service_observations_omitted"] == 2


def test_global_settings_uses_only_validated_local_namespaces_or_explicit_filter(environment, monkeypatch):
    _, data, backend = environment
    provider = Mock(return_value={"read_only": True})
    monkeypatch.setitem(sys.modules, "podgrove.web_settings", SimpleNamespace(settings=provider))
    monkeypatch.setattr(backend, "_records", lambda: [{"data": data}, {"data": data},
                                                   {"path": Path("invalid.json"), "error": "untrusted state"}])
    assert backend.settings() == {"read_only": True}
    provider.assert_called_once_with(data["context"], ["default"], namespace=None)
    provider.reset_mock()
    explicit = web.Dashboard(data["context"], "podgrove-testing")
    monkeypatch.setattr(explicit, "_records", Mock(side_effect=AssertionError("Explicit namespace needs no record scan")))
    explicit.settings(namespace="podgrove-testing")
    provider.assert_called_once_with(data["context"], ["podgrove-testing"], namespace="podgrove-testing")


def test_global_settings_http_route_keeps_auth_and_only_allows_namespace_selection(server):
    instance, backend = server
    backend.settings.return_value = {"read_only": True, "selected_namespace": "default"}
    assert request(instance, "/api/settings")[0] == 403
    backend.settings.assert_not_called()
    code, _, body = request(instance, "/api/settings?namespace=default", headers={"X-Podgrove-Token": instance.token})
    assert code == 200 and json.loads(body)["selected_namespace"] == "default"
    backend.settings.assert_called_once_with(namespace="default")
    backend.reset_mock()
    for path in ("/api/settings?namespace=default&namespace=podgrove-testing", "/api/settings?context=other", "/api/settings?exec=apply"):
        assert request(instance, path, headers={"X-Podgrove-Token": instance.token})[0] == 400
    backend.settings.assert_not_called()
    backend.settings.side_effect = web.WebError("Namespace is outside the verified local inventory", 400)
    code, _, body = request(instance, "/api/settings?namespace=kube-system", headers={"X-Podgrove-Token": instance.token})
    assert code == 400 and b"outside the verified" in body


@pytest.mark.parametrize("source", ["engine", "service"])
def test_all_retained_log_snapshot_keeps_byte_limit_and_redaction(engine, monkeypatch, source):
    data, backend, _, _, _ = engine
    secret_line = b"password=do-not-display\n"
    payload = secret_line + b"x" * (web.MAX_LOG - len(secret_line))
    command = Mock(return_value=payload)
    docker = Mock(return_value=(payload, True))
    monkeypatch.setattr(web, "bounded_read_command", command)
    monkeypatch.setattr(backend, "_docker", docker)
    result = backend.logs(data["identity"], source=source, service="api" if source == "service" else None, tail="all")
    assert result["tail"] == "all" and result["truncated"] is True
    assert "do-not-display" not in result["text"] and "[REDACTED]" in result["text"]
    if source == "engine":
        args = command.call_args.args[0]
        assert args[args.index("--tail") + 1] == "-1"
        assert args[args.index("--limit-bytes") + 1] == str(web.MAX_LOG)
        assert command.call_args.kwargs["limit"] == web.MAX_LOG
        docker.assert_not_called()
    else:
        assert "tail=all" in docker.call_args.args[1]
        assert docker.call_args.kwargs["limit"] == web.MAX_LOG
        assert docker.call_args.kwargs["allow_truncated"] is True
        command.assert_not_called()


@pytest.mark.parametrize("tail", [True, False, 1.0, "100", "ALL", "all ", "-1", -1, None, []])
def test_snapshot_tail_rejects_non_integer_and_non_all_values_before_reads(engine, monkeypatch, tail):
    data, backend, *_ = engine
    monkeypatch.setattr(backend, "_record", Mock(side_effect=AssertionError("No invalid source read")))
    with pytest.raises(web.WebError) as caught:
        backend.logs(data["identity"], source="engine", service=None, tail=tail)
    assert caught.value.status == 400


@pytest.mark.parametrize("source", ["engine", "service"])
def test_http_snapshot_accepts_all_retained_history(server, source):
    instance, backend = server
    path = f"/api/environments/123456abcdef/logs?source={source}&tail=all"
    service = "api" if source == "service" else None
    if service:
        path += "&service=" + service
    code, _, _ = request(instance, path, headers={"X-Podgrove-Token": instance.token})
    assert code == 200
    backend.logs.assert_called_once_with("123456abcdef", source=source, service=service, tail="all")


@pytest.mark.parametrize("tail", ["ALL", "all%20", "-1", "0", "201", "1000", "1.0", "true", "all&tail=100", ""])
def test_http_snapshot_invalid_all_tail_is_rejected_before_dispatch(server, tail):
    instance, backend = server
    code, _, _ = request(instance, f"/api/environments/123456abcdef/logs?tail={tail}",
                         headers={"X-Podgrove-Token": instance.token})
    assert code == 400
    backend.logs.assert_not_called()
