"""Real local streaming, framing, ownership, cancellation, and HTTP isolation."""
from contextlib import contextmanager
from copy import deepcopy
import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from podgrove import state, web, web_logs
from podgrove.kube import Kube
from test_web import environment as environment, engine as engine


def frame(raw, stream=1):
    return bytes([stream, 0, 0, 0]) + len(raw).to_bytes(4, "big") + raw


@pytest.fixture
def follower(engine, monkeypatch):
    data, backend, objects, reads, rows = engine
    read = backend._kube_read
    monkeypatch.setattr(backend, "_kube_read", lambda *args, **_kwargs: read(*args))
    monkeypatch.setattr(web_logs, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(web_logs, "VERIFY_SECONDS", 0.1)
    return data, backend, objects, reads, rows


def engine_command(monkeypatch, script):
    commands = []

    def command(_self, *args):
        commands.append(args)
        return [sys.executable, "-u", "-c", script]

    monkeypatch.setattr(Kube, "command", command)
    return commands


def stream_for(follower, **kwargs):
    data, backend, *_ = follower
    return backend.logs_stream(data["identity"], source=kwargs.pop("source", "engine"),
                               service=kwargs.pop("service", None), tail=kwargs.pop("tail", 100), **kwargs)


@contextmanager
def serve(backend):
    server = web.DashboardServer("test-context", backend=backend)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def request(server, ident, query="", *, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    connection.request("GET", f"/api/environments/{ident}/logs/stream{query}",
                       headers=headers if headers is not None else {"X-Podgrove-Token": server.token})
    return connection, connection.getresponse()


def event(response):
    raw = response.readline()
    assert raw, "Stream ended before the expected record"
    return json.loads(raw)


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_engine_follows_real_process_without_tail_polling_or_activity_writes(follower, monkeypatch):
    data, _, _, _, _ = follower
    before = state.state_path(Path(data["root"]), data["context"]).read_bytes()
    commands = engine_command(monkeypatch, "import time; print('first'); time.sleep(.15); print('second')")
    monkeypatch.setattr(state, "write", Mock(side_effect=AssertionError("No activity writes")))
    stream = stream_for(follower)
    try:
        start = stream.start()
        assert start["type"] == "start" and start["resume"] == "restart_with_tail" and start["gap_possible"]
        records = list(stream.events())
    finally:
        stream.close()
    assert [item["text"] for item in records if item["type"] == "line"] == ["first\n", "second\n"]
    assert records[-1]["reason"] == "completed"
    assert len(commands) == 1 and "--follow" in commands[0] and "--tail" in commands[0]
    assert state.state_path(Path(data["root"]), data["context"]).read_bytes() == before
    assert not any(thread.is_alive() for thread in stream.threads)


@pytest.mark.parametrize("cut", range(1, 17))
def test_docker_headers_utf8_and_credentials_are_safe_across_arbitrary_chunk_boundaries(cut):
    records = []
    lines = web_logs.LineBuffer(records.append, web._redact_logs, "a" * 64)
    decoder = web_logs.DockerFrames(lines.feed)
    payload = frame(b"pass") + frame("word=sensitive café\n".encode()) + frame(b"stderr ready\n", 2)
    for offset in range(0, len(payload), cut):
        decoder.feed(payload[offset:offset + cut])
    decoder.finish()
    lines.finish()
    assert records[0]["text"] == "password=[REDACTED] café\n"
    assert "sensitive" not in json.dumps(records)
    assert records[1]["stream"] == "stderr" and records[1]["text"] == "stderr ready\n"
    assert lines.partial_lines_discarded == 0


def test_channels_do_not_join_secrets_with_other_streams_and_partial_lines_never_escape():
    records = []
    lines = web_logs.LineBuffer(records.append, web._redact_logs)
    lines.feed("stdout", b"token=")
    lines.feed("stderr", b"status okay\n")
    lines.feed("stdout", b"private-token\nsecret=incomplete-secret")
    lines.finish()
    assert [row["text"] for row in records] == ["status okay\n", "token=[REDACTED]\n"]
    assert lines.partial_lines_discarded == 1


@pytest.mark.parametrize("header", ["Authorization", "Proxy-Authorization"])
def test_basic_authorization_is_fully_redacted_for_snapshot_and_split_live_logs(header):
    credential = base64.b64encode(b"fixture:credential").decode()
    text = f"{header}: Basic {credential}\n"
    assert credential not in web._redact_logs(text)
    records = []
    lines = web_logs.LineBuffer(records.append, web._redact_logs)
    decoder = web_logs.DockerFrames(lines.feed)
    data = frame(text[:15].encode()) + frame(text[15:].encode())
    for character in data:
        decoder.feed(bytes([character]))
    assert credential not in json.dumps(records)
    assert len(records) == 1 and "[REDACTED]" in records[0]["text"]


def test_oversized_lines_are_discarded_as_a_whole_and_following_lines_continue():
    records = []
    lines = web_logs.LineBuffer(records.append, web._redact_logs)
    lines.feed("stdout", b"token=" + b"x" * (web_logs.MAX_LINE_BYTES * 3))
    assert not records and len(lines.pending["stdout"]) == 0
    lines.feed("stdout", b"secret-ending\nok\n")
    assert records[0]["reason"] == "line_limit"
    assert records[1]["text"] == "ok\n"
    assert "secret-ending" not in json.dumps(records)


def test_private_key_blocks_are_not_exposed_across_lines():
    records = []
    lines = web_logs.LineBuffer(records.append, web._redact_logs)
    text = "-----BEGIN " + "PRIVATE KEY-----\nprivate-material\n-----END " + "PRIVATE KEY-----\nready\n"
    for character in text.encode():
        lines.feed("stdout", bytes([character]))
    assert "private-material" not in json.dumps(records)
    assert records[-1]["text"] == "ready\n"


@pytest.mark.parametrize("marker_position", ["before_limit", "after_limit"])
def test_oversized_split_key_delimiters_keep_redacting_until_the_exact_end(marker_position):
    records = []
    lines = web_logs.LineBuffer(records.append, web._redact_logs)
    begin = ("-----BEGIN " + "PRIVATE KEY-----").encode()
    end = ("-----END " + "PRIVATE KEY-----").encode()
    filler = b"x" * (web_logs.MAX_LINE_BYTES + 10)
    opening = begin + filler if marker_position == "before_limit" else filler + begin
    # Delimiter bytes are deliberately split across decoder deliveries.
    for offset in range(0, len(opening), 7):
        lines.feed("stdout", opening[offset:offset + 7])
    lines.feed("stdout", b"\nprivate-body\n" + filler)
    for character in end:
        lines.feed("stdout", bytes([character]))
    lines.feed("stdout", b"\nnormal again\n")
    assert "private-body" not in json.dumps(records)
    assert [row["type"] for row in records] == ["notice", "line", "notice", "line"]
    assert records[1]["text"] == "[REDACTED PRIVATE KEY]\n"
    assert records[-1]["text"] == "normal again\n"
    assert len(lines.marker_tail["stdout"]) <= 128


@pytest.mark.parametrize("payload", [b"\x01\0", b"\x01\0\0\0\0\0\0\x09short",
                                      b"\x03\0\0\0\0\0\0\x03bad",
                                      b"\x01BAD\0\0\0\x03bad",
                                      b"\x01\0\0\0\x7f\xff\xff\xff"])
def test_incomplete_invalid_and_oversized_frames_refuse(payload):
    decoder = web_logs.DockerFrames(lambda *_: None)
    with pytest.raises(web_logs.FollowError):
        decoder.feed(payload)
        decoder.finish()


def test_plain_tty_text_with_short_initial_chunk_is_supported():
    records = []
    lines = web_logs.LineBuffer(records.append, web._redact_logs)
    decoder = web_logs.DockerFrames(lines.feed)
    decoder.feed(b"ok\n")
    assert records[0]["text"] == "ok\n"
    decoder.finish()


@pytest.mark.parametrize("kwargs", [
    {"tail": 0}, {"tail": 201}, {"tail": True}, {"source": "exec"}, {"service": "api"},
    {"source": "service", "service": "../api"}, {"source": "service", "service": "api", "container": "--bad"},
    {"container": "a" * 64},
])
def test_invalid_parameters_fail_before_source_reads(follower, monkeypatch, kwargs):
    monkeypatch.setattr(follower[1], "_record", Mock(side_effect=AssertionError("No invalid request reads")))
    with pytest.raises(web.WebError) as caught:
        stream_for(follower, **kwargs)
    assert caught.value.status == 400


@pytest.mark.parametrize("change", ["pod", "statefulset", "persistentvolumeclaim", "local_binding"])
def test_ownership_change_ends_a_quiet_stream_and_reaps_source(follower, monkeypatch, change):
    data, _, objects, *_ = follower
    engine_command(monkeypatch, "import time; time.sleep(30)")
    stream = stream_for(follower)
    stream.start()
    if change == "local_binding":
        updated = {**data, "docker_host": "tcp://127.0.0.1:1"}
        state.write(state.state_path(Path(data["root"]), data["context"]), updated)
    else:
        objects[change]["metadata"]["uid"] = "replacement"
    records = list(stream.events())
    assert records[-1]["reason"] == "ownership_changed"
    assert not any(row["type"] == "line" for row in records)
    assert not any(thread.is_alive() for thread in stream.threads)


def test_first_log_bytes_are_not_emitted_if_pod_changed_while_kubectl_opened(follower, monkeypatch):
    engine_command(monkeypatch, "import time; time.sleep(.15); print('must stay private'); time.sleep(30)")
    stream = stream_for(follower)
    stream.verify_seconds = 10
    stream.start()
    follower[2]["pod"]["metadata"]["uid"] = "replacement"
    records = list(stream.events())
    assert records[-1]["reason"] == "ownership_changed"
    assert "must stay private" not in json.dumps(records)


def test_lifetime_expiry_is_explicit_and_cleans_up_quiet_source(follower, monkeypatch):
    engine_command(monkeypatch, "import time; time.sleep(30)")
    stream = stream_for(follower)
    stream.max_seconds = 0.15
    stream.start()
    records = list(stream.events())
    assert any(row["type"] == "heartbeat" for row in records)
    assert records[-1]["reason"] == "lifetime_limit" and records[-1]["gap_possible"]
    assert not any(thread.is_alive() for thread in stream.threads)


def test_slow_consumer_has_bounded_queue_and_can_cancel(follower, monkeypatch):
    engine_command(monkeypatch, "while True: print('x'*1000)")
    stream = stream_for(follower)
    stream.start()
    wait_until(lambda: stream.records.full())
    assert stream.records.qsize() == web_logs.QUEUE_LINES
    stream.close()
    assert not any(thread.is_alive() for thread in stream.threads)


def test_http_stream_auth_security_headers_slots_and_normal_get_responsiveness(follower, monkeypatch):
    data, backend, *_ = follower
    engine_command(monkeypatch, "import time; time.sleep(30)")
    with serve(backend) as server:
        readers = []
        for _ in range(2):
            connection, response = request(server, data["identity"])
            readers.append((connection, response))
            assert response.status == 200
            assert response.getheader("Content-Type").startswith("application/x-ndjson")
            assert response.getheader("Cache-Control") == "no-store"
            assert event(response)["type"] == "start"
        try:
            assert server.request_slots._value == 4
            extra, refusal = request(server, data["identity"])
            assert refusal.status == 429
            refusal.read()
            extra.close()
            ordinary = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=1)
            ordinary.request("GET", "/api/environments", headers={"X-Podgrove-Token": server.token})
            result = ordinary.getresponse()
            assert result.status == 200 and json.loads(result.read())["environments"]
            ordinary.close()
        finally:
            for connection, response in readers:
                response.close()
                connection.close()
        wait_until(lambda: not server.active_streams)
        assert server.stream_slots._value == 2


@pytest.mark.parametrize("headers,query,status", [
    ({}, "", 403), ({"X-Podgrove-Token": "wrong"}, "", 403),
    ({"Origin": "https://foreign.invalid"}, "", 403),
    ({"Host": "foreign.invalid"}, "", 403),
    ({"Sec-Fetch-Site": "cross-site"}, "", 403),
    (None, "?tail=0", 400), (None, "?tail=201", 400), (None, "?tail=100&tail=100", 400),
    (None, "?unknown=1", 400), (None, "?source=service", 400),
    (None, "?source=engine&container=" + "a" * 64, 400),
])
def test_http_invalid_requests_do_not_start_readers(follower, monkeypatch, headers, query, status):
    data, backend, *_ = follower
    monkeypatch.setattr(backend, "_engine", Mock(side_effect=AssertionError("No invalid source read")))
    with serve(backend) as server:
        selected = headers
        if headers and "X-Podgrove-Token" not in headers:
            selected = {**headers, "X-Podgrove-Token": server.token}
        connection, response = request(server, data["identity"], query, headers=selected)
        assert response.status == status
        response.read()
        connection.close()
        assert not server.active_streams


def test_server_shutdown_ends_active_stream_and_cleans_up(follower, monkeypatch):
    data, backend, *_ = follower
    engine_command(monkeypatch, "import time; time.sleep(30)")
    with serve(backend) as server:
        connection, response = request(server, data["identity"])
        assert event(response)["type"] == "start"
        sources = list(server.active_streams)
        server.shutdown()
        server.server_close()
        records = [json.loads(line) for line in response]
        assert records[-1]["reason"] == "server_shutdown"
        assert all(not thread.is_alive() for source in sources for thread in source.threads)
        response.close()
        connection.close()


def test_browser_disconnect_cancels_inflight_ownership_subprocess(follower, monkeypatch):
    data, backend, *_ = follower
    engine_command(monkeypatch, "import time; time.sleep(30)")
    original = backend._engine
    verifying = threading.Event()
    calls = [0]
    stopped_processes = []
    stop = web._stop

    def tracked_stop(process):
        stop(process)
        stopped_processes.append(process)

    def verify(*args, cancel=None, **kwargs):
        calls[0] += 1
        if calls[0] >= 3:
            verifying.set()
            web.bounded_read_command([sys.executable, "-c", "import time; time.sleep(30)"], cancel=cancel)
        return original(*args, cancel=cancel, **kwargs)

    monkeypatch.setattr(backend, "_engine", verify)
    monkeypatch.setattr(web, "_stop", tracked_stop)
    with serve(backend) as server:
        connection, response = request(server, data["identity"])
        assert event(response)["type"] == "start"
        assert verifying.wait(1)
        response.close()
        connection.close()
        wait_until(lambda: not server.active_streams, timeout=1)
        assert stopped_processes and all(process.poll() is not None for process in stopped_processes)


@pytest.mark.parametrize("kind", ["pod", "statefulset", "persistentvolumeclaim"])
def test_initial_foreign_ownership_refuses_before_launch(follower, monkeypatch, kind):
    follower[2][kind]["metadata"]["labels"]["podgrove.dev/environment"] = "foreign"
    monkeypatch.setattr(web_logs.subprocess, "Popen", Mock(side_effect=AssertionError("No source launch")))
    with pytest.raises(web.WebError):
        stream_for(follower).start()


@contextmanager
def docker_source(backend, monkeypatch, chunks, *, quiet=False, status=200, trickle_headers=False):
    calls, closed = [], threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            calls.append(self.path)
            if trickle_headers:
                try:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    while True:
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.01)
                except OSError:
                    closed.set()
                    return
            self.send_response(status)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    time.sleep(0.01)
                if quiet:
                    self.connection.settimeout(3)
                    while self.connection.recv(1):
                        pass
            except OSError:
                pass
            finally:
                closed.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.start()

    def connect(_data, *, timeout):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
        connection.connect()
        return connection

    monkeypatch.setattr(backend, "_docker_connection", connect)
    try:
        yield calls, closed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_service_follows_real_http_frames_and_redacts_split_utf8(follower, monkeypatch):
    payload = frame(b"api_key=") + frame("hidden café\n".encode()) + frame(b"ready\n", 2)
    with docker_source(follower[1], monkeypatch, [payload[index:index + 3] for index in range(0, len(payload), 3)]) as (calls, _):
        stream = stream_for(follower, source="service", service="api")
        stream.start()
        records = list(stream.events())
    assert [row["text"] for row in records if row["type"] == "line"] == ["api_key=[REDACTED] café\n", "ready\n"]
    assert len(calls) == 1 and "follow=1" in calls[0] and "tail=100" in calls[0]
    assert records[-1]["reason"] == "completed"
    assert not any(thread.is_alive() for thread in stream.threads)


def test_quiet_docker_socket_cancels_without_waiting_for_log_output(follower, monkeypatch):
    with docker_source(follower[1], monkeypatch, [], quiet=True) as (_, closed):
        stream = stream_for(follower, source="service", service="api")
        stream.start()
        stream.close()
        assert closed.wait(1)
        assert not any(thread.is_alive() for thread in stream.threads)


def test_short_tty_line_arrives_while_the_real_http_source_remains_open(follower, monkeypatch):
    with docker_source(follower[1], monkeypatch, [b"ok\n"], quiet=True) as (_, closed):
        stream = stream_for(follower, source="service", service="api")
        stream.start()
        records = stream.events()
        try:
            first = next(records)
            assert first["type"] == "line" and first["text"] == "ok\n"
            assert not closed.is_set()
        finally:
            stream.close()
            records.close()
        assert closed.wait(1)


def test_docker_non_success_never_emits_source_body(follower, monkeypatch):
    with docker_source(follower[1], monkeypatch, [b"private failure body"], status=403):
        stream = stream_for(follower, source="service", service="api")
        with pytest.raises(web.WebError) as caught:
            stream.start()
        assert "private failure body" not in str(caught.value)
        assert not any(thread.is_alive() for thread in stream.threads)


def test_trickling_docker_headers_have_total_deadline_and_no_leftover_reader(follower, monkeypatch):
    monkeypatch.setattr(web_logs, "HEADER_SECONDS", 0.15)
    with docker_source(follower[1], monkeypatch, [], trickle_headers=True) as (_, closed):
        stream = stream_for(follower, source="service", service="api")
        started = time.monotonic()
        with pytest.raises(web.WebError):
            stream.start()
        assert time.monotonic() - started < 1
        assert closed.wait(1)
        assert not any(thread.is_alive() for thread in stream.threads)


def test_pre_cancelled_stream_does_not_open_a_source(follower, monkeypatch):
    stream = stream_for(follower)
    stream.close()
    monkeypatch.setattr(follower[1], "_record", Mock(side_effect=AssertionError("No cancelled source read")))
    with pytest.raises(web.WebError, match="cancelled"):
        stream.start()


def test_replica_limit_and_explicit_container_selection(follower, monkeypatch):
    rows = follower[4]
    original = deepcopy(rows[0])
    rows[:] = [{**original, "Id": f"{number:064x}"} for number in range(9)]
    with pytest.raises(web.WebError) as caught:
        stream_for(follower, source="service", service="api").start()
    assert caught.value.status == 409
    with docker_source(follower[1], monkeypatch, [frame(b"one\n")]) as (calls, _):
        stream = stream_for(follower, source="service", service="api", container=rows[3]["Id"])
        assert stream.start()["containers"] == [rows[3]["Id"]]
        list(stream.events())
    assert len(calls) == 1 and rows[3]["Id"] in calls[0]


def test_snapshot_container_selection_matches_live_source_and_refuses_foreign_id(follower, monkeypatch):
    data, backend, _, _, rows = follower
    rows.append({**rows[0], "Id": "c" * 64})
    read = Mock(return_value=(frame(b"selected\n"), False))
    monkeypatch.setattr(backend, "_docker", read)
    result = backend.logs(data["identity"], source="service", service="api", tail=10, container="c" * 64)
    assert result["text"] == "selected\n"
    assert len(read.call_args_list) == 1 and "c" * 64 in read.call_args.args[1]
    with pytest.raises(web.WebError) as caught:
        backend.logs(data["identity"], source="service", service="api", tail=10, container="d" * 64)
    assert caught.value.status == 404
    with pytest.raises(web.WebError) as caught:
        backend.logs(data["identity"], source="engine", service=None, tail=10, container="c" * 64)
    assert caught.value.status == 400


@pytest.mark.parametrize("layout,expected", [("plain", None), ("normal", "feature/live-logs"),
                                             ("linked", "feature/other"), ("detached", "detached-" + "a" * 12)])
def test_summary_reads_current_bounded_metadata_without_git_or_env_overrides(environment, monkeypatch, layout, expected):
    root, data, backend = environment
    if layout in ("normal", "detached"):
        (root / ".git").mkdir()
        (root / ".git/HEAD").write_text("a" * 40 if layout == "detached" else "ref: refs/heads/feature/live-logs\n")
    elif layout == "linked":
        common = root.parent / "repository/.git"
        linked = common / "worktrees/linked"
        linked.mkdir(parents=True)
        (root / ".git").write_text(f"gitdir: {linked}\n")
        (linked / "commondir").write_text("../..\n")
        (linked / "HEAD").write_text("ref: refs/heads/feature/other\n")
    monkeypatch.setenv("PODGROVE_REPO", "wrong-repo")
    monkeypatch.setenv("PODGROVE_BRANCH", "wrong-branch")
    monkeypatch.setattr(web.subprocess, "Popen", Mock(side_effect=AssertionError("No Git subprocess")))
    monkeypatch.setattr(state, "write", Mock(side_effect=AssertionError("No state writes")))
    result = backend._summary(data)
    assert result["branch"] == expected and result["worktree"] == root.name
    assert result["repository"] == ("repository" if layout == "linked" else root.name)
    assert result["context"] == data["context"]


def test_bounded_ownership_read_cancels_its_process_without_disclosing_stderr():
    stopped = threading.Event()
    timer = threading.Timer(0.05, stopped.set)
    timer.start()
    try:
        with pytest.raises(web.WebError, match="cancelled"):
            web.bounded_read_command([sys.executable, "-c", "import time; time.sleep(30)"], cancel=stopped)
    finally:
        timer.cancel()
        timer.join()


@pytest.mark.parametrize("source", ["engine", "service"])
def test_http_all_retained_history_stream_maps_source_tail_and_redacts(follower, monkeypatch, source):
    data, backend, *_ = follower
    commands = engine_command(monkeypatch, "print('password=hidden-history'); print('last retained line')")
    with docker_source(backend, monkeypatch, [frame(b"password=hidden-history\nlast retained line\n")]) as (calls, _):
        with serve(backend) as server:
            query = f"?source={source}&tail=all" + ("&service=api" if source == "service" else "")
            connection, response = request(server, data["identity"], query)
            try:
                assert response.status == 200
                start = event(response)
                assert start["type"] == "start" and start["tail"] == "all"
                records = [json.loads(line) for line in response]
            finally:
                response.close()
                connection.close()
            wait_until(lambda: not server.active_streams)
    assert [row["text"] for row in records if row["type"] == "line"] == [
        "password=[REDACTED]\n", "last retained line\n"]
    assert records[-1]["reason"] == "completed"
    if source == "engine":
        assert not calls and len(commands) == 1
        assert commands[0][commands[0].index("--tail") + 1] == "-1"
        assert "--follow" in commands[0]
    else:
        assert not commands and len(calls) == 1
        assert "tail=all" in calls[0] and "follow=1" in calls[0]


@pytest.mark.parametrize("tail", ["ALL", "all%20", "-1", "0", "201", "1000", "1.0", "true", "all&tail=100", ""])
def test_http_stream_invalid_all_tail_is_rejected_before_reads(follower, monkeypatch, tail):
    data, backend, *_ = follower
    read = Mock(side_effect=AssertionError("No invalid source read"))
    monkeypatch.setattr(backend, "_record", read)
    with serve(backend) as server:
        connection, response = request(server, data["identity"], "?tail=" + tail)
        try:
            assert response.status == 400
            response.read()
        finally:
            connection.close()
        assert not server.active_streams and server.stream_slots._value == 2
    read.assert_not_called()


@pytest.mark.parametrize("tail", [False, 1.0, "100", "ALL", "all ", "-1", -1, None, []])
def test_stream_tail_rejects_non_integer_and_non_all_values(follower, monkeypatch, tail):
    monkeypatch.setattr(follower[1], "_record", Mock(side_effect=AssertionError("No invalid source read")))
    with pytest.raises(web.WebError) as caught:
        stream_for(follower, tail=tail)
    assert caught.value.status == 400
