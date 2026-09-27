"""Guarded soak runner tests: local fixtures only, no GitHub or Kubernetes."""
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location("soak_forwarding", Path(__file__).resolve().parents[1] / "scripts/soak_forwarding.py")
soak = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(soak)


@pytest.fixture
def fixture(tmp_path):
    project = (tmp_path / "project").resolve()
    project.mkdir()
    state_home = tmp_path / "state"
    state_home.mkdir(mode=0o700)
    marker = project / "marker.txt"
    marker.write_text("fixture-ready\n")
    kubeconfig = tmp_path / "client.kubeconfig"
    kubeconfig.write_text("fixture-credential-never-persist")
    kubeconfig.chmod(0o600)
    binary = tmp_path / "release/venv/bin/podgrove"
    binary.parent.mkdir(parents=True)
    binary.write_text("fixture")
    identity = hashlib.sha256(str(project).encode()).hexdigest()[:12]
    args = SimpleNamespace(binary=binary, wheel=tmp_path / "podgrove-0.2.0-py3-none-any.whl", project_directory=project, state_home=state_home,
                           kubeconfig=kubeconfig, marker_file=marker, output=tmp_path / "evidence",
                           context="fixture:context", namespace="fixture-owned", identity=identity,
                           url="http://127.0.0.1:23456/", endpoint=soak.urlsplit("http://127.0.0.1:23456/"),
                           duration=.08, probe_interval=.02, status_interval=.02, inject_after=None, recovery_timeout=1)
    data = {"root": str(project), "identity": identity, "context": args.context, "namespace": args.namespace,
            "pid": 12000, "token": "fixture-state-secret", "socket": "fixture-private-socket", "docker_host": "fixture-private-host",
            "ports": [{"local": 23456, "published": 8080, "target": 8000, "service": "probe", "status": "ready"}]}
    path = state_home / f"{identity}-{hashlib.sha256(args.context.encode()).hexdigest()[:8]}.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    proof = {"binary": str(binary), "python": str(binary.parent / "python"), "version": "0.2.0",
             "source_commit": "a" * 40, "package_sha256": "b" * 64, "editable": False}
    table = {
        12000: {"pid": 12000, "parent": 1, "uid": os.getuid(), "started": "Sun Sep 27 12:00:00 2026",
                "argv": [proof["python"], "-m", "podgrove", "_serve", str(path)]},
        12001: {"pid": 12001, "parent": 12000, "uid": os.getuid(), "started": "Sun Sep 27 12:00:01 2026",
                "argv": ["kubectl", "--context", args.context, "--namespace", args.namespace, "--request-timeout=30s",
                         "port-forward", f"pod/pg-{identity}-0", "--address=127.0.0.1", "--request-timeout=0", "23456:8080"]},
    }
    return args, data, path, proof, table


def mocked_observations(monkeypatch, fixture):
    args, data, path, proof, table = fixture
    monkeypatch.setattr(soak, "binary_proof", lambda *_, **_kwargs: deepcopy(proof))
    monkeypatch.setattr(soak, "owned_uids", lambda *_: {"pod": "pod-uid", "pvc": "pvc-uid", "statefulset": "sts-uid"})
    monkeypatch.setattr(soak, "process_table", lambda *_: deepcopy(table))
    monkeypatch.setattr(soak, "sample_status", lambda *_: (True, {"exit_code": 0, "status": "ready", "forward_state": "ready"}))
    monkeypatch.setattr(soak, "http_probe", lambda *_: True)
    return args, data, path, proof, table


def read_result(args):
    return json.loads((args.output / "result.json").read_text())


def test_short_run_records_private_evidence_without_claiming_four_hours(fixture, monkeypatch):
    args, *_ = mocked_observations(monkeypatch, fixture)
    assert soak.run(args, threading.Event()) == 0
    result = read_result(args)
    assert result["passed"] and not result["four_hour_proof"]
    assert result["elapsed_seconds"] >= args.duration
    assert result["counts"]["http"] >= 2 and result["counts"]["status"] >= 2 and result["counts"]["errors"] == 0
    assert result["binary"]["editable"] is False and result["resource_uids"]["pod"] == "pod-uid"
    assert args.output.stat().st_mode & 0o777 == 0o700
    for path in args.output.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        for hidden in ("fixture-state-secret", "fixture-private-socket", "fixture-private-host", "fixture-credential-never-persist"):
            assert hidden not in path.read_text()
    rows = [json.loads(line) for line in (args.output / "samples.jsonl").read_text().splitlines()]
    assert rows[0]["kind"] == "start" and rows[-1]["kind"] == "end"
    assert all("utc" in row and "elapsed_seconds" in row for row in rows)


def test_existing_evidence_directory_is_never_reused(fixture, monkeypatch):
    args, *_ = fixture
    args.output.mkdir()
    (args.output / "keep").write_text("original")
    monkeypatch.setattr(soak, "binary_proof", Mock(side_effect=AssertionError("Must refuse before observations")))
    with pytest.raises(FileExistsError):
        soak.run(args, threading.Event())
    assert (args.output / "keep").read_text() == "original"


def test_http_failure_and_cancellation_never_pass(fixture, monkeypatch):
    args, *_ = mocked_observations(monkeypatch, fixture)
    stopped = threading.Event()
    def probe(*_):
        stopped.set()
        return False
    monkeypatch.setattr(soak, "http_probe", probe)
    assert soak.run(args, stopped) == 1
    result = read_result(args)
    assert result["cancelled"] and not result["passed"] and not result["four_hour_proof"]
    assert result["counts"]["errors"] == 1


@pytest.mark.parametrize("kind", ["runtime", "uids", "binding"])
def test_changed_runtime_resource_or_supervisor_fails_closed(fixture, monkeypatch, kind):
    args, data, path, proof, _ = mocked_observations(monkeypatch, fixture)
    if kind == "runtime":
        calls = iter([proof, {**proof, "package_sha256": "changed"}])
        monkeypatch.setattr(soak, "binary_proof", lambda *_, **_kwargs: next(calls))
    elif kind == "uids":
        calls = iter([{"pod": "first"}, {"pod": "replacement"}])
        monkeypatch.setattr(soak, "owned_uids", lambda *_: next(calls))
    else:
        def processes(*_):
            data["pid"] += 1
            path.write_text(json.dumps(data))
            return fixture[-1]
        monkeypatch.setattr(soak, "process_table", processes)
    assert soak.run(args, threading.Event()) == 1
    assert not read_result(args)["passed"]


def test_status_forward_state_transition_is_not_a_changed_resource_binding(fixture):
    _, data, *_ = fixture
    changed = deepcopy(data)
    changed["ports"][0]["status"] = "reconnecting"
    assert soak.state_binding(data) == soak.state_binding(changed)
    changed["ports"][0]["published"] += 1
    assert soak.state_binding(data) != soak.state_binding(changed)


@pytest.mark.parametrize("change", ["namespace", "context", "identity", "root", "pid", "port"])
def test_state_scope_and_url_are_exact(fixture, change):
    args, data, path, *_ = fixture
    if change == "port":
        data["ports"][0]["local"] += 1
    elif change == "pid":
        data[change] = True
    else:
        data[change] = "foreign"
    path.write_text(json.dumps(data))
    with pytest.raises(soak.Refused):
        soak.read_state(args)


@pytest.mark.parametrize("change", ["namespace", "context", "pod", "port", "parent", "uid", "supervisor-runtime", "duplicate"])
def test_fault_selection_refuses_any_unproven_process(fixture, change):
    args, data, path, proof, original = fixture
    table = deepcopy(original)
    child = table[12001]
    if change in ("namespace", "context"):
        index = child["argv"].index("--" + change) + 1
        child["argv"][index] = "foreign"
    elif change == "pod":
        child["argv"][7] = "pod/foreign"
    elif change == "port":
        child["argv"][-1] = "23456:9999"
    elif change == "parent":
        child["parent"] = 99999
    elif change == "uid":
        child["uid"] += 1
    elif change == "supervisor-runtime":
        table[12000]["argv"][0] = "/checkout/.venv/bin/python"
    else:
        table[12002] = {**deepcopy(child), "pid": 12002}
    with pytest.raises(soak.Refused):
        soak.forward_process(table, args, data, path, proof)


def test_fault_is_one_exact_child_signal_and_requires_new_healthy_process(fixture, monkeypatch):
    args, data, path, proof, table = mocked_observations(monkeypatch, fixture)
    args.duration, args.inject_after = .12, .01
    killed = []
    def kill(pid, sig):
        killed.append((pid, sig))
        child = table.pop(pid)
        table[13001] = {**child, "pid": 13001, "started": "Sun Sep 27 12:01:00 2026"}
        data["ports"][0]["status"] = "reconnecting"
        path.write_text(json.dumps(data))
    monkeypatch.setattr(soak.os, "kill", kill)
    assert soak.run(args, threading.Event()) == 0
    result = read_result(args)
    assert killed == [(12001, signal.SIGTERM)]
    assert result["fault"]["status"] == "recovered" and result["fault"]["replacement"]["pid"] == 13001
    assert result["fault"]["supervisor_pid"] == 12000
    assert not result["four_hour_proof"]


def test_changed_child_between_fault_proofs_is_not_signalled(fixture, monkeypatch):
    args, *_ = mocked_observations(monkeypatch, fixture)
    args.inject_after = 0
    calls = 0
    original = soak.forward_process
    def selected(*values):
        nonlocal calls
        calls += 1
        result = original(*values)
        if calls == 3:
            result["started"] = "different process"
        return result
    monkeypatch.setattr(soak, "forward_process", selected)
    kill = Mock(side_effect=AssertionError("No ambiguous process signal"))
    monkeypatch.setattr(soak.os, "kill", kill)
    assert soak.run(args, threading.Event()) == 1
    assert read_result(args)["fault"]["status"] == "skipped"
    kill.assert_not_called()


def test_bounded_subprocess_and_post_eof_cancellation():
    stop = threading.Event()
    command = [sys.executable, "-c", "import os,time;os.close(1);time.sleep(30)"]
    timer = threading.Timer(.1, stop.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(soak.Refused, match="cancelled"):
            soak.read_command(command, dict(os.environ), stop, timeout=10)
    finally:
        timer.cancel()
        timer.join()
    assert time.monotonic() - started < 2
    with pytest.raises(soak.Refused, match="output limit"):
        soak.read_command([sys.executable, "-c", "print('x'*10000)"], dict(os.environ), threading.Event(), limit=100)


def test_real_http_fixture_probe_checks_marker_without_following_redirects():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://example.invalid/")
                self.end_headers()
                return
            raw = json.dumps({"ok": True, "marker": "fixture"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    thread.start()
    try:
        endpoint = soak.urlsplit(f"http://127.0.0.1:{server.server_port}/")
        assert soak.http_probe(endpoint, "fixture")
        assert not soak.http_probe(endpoint, "wrong")
        assert not soak.http_probe(soak.urlsplit(f"http://127.0.0.1:{server.server_port}/redirect"), "fixture")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_status_evidence_projection_never_keeps_private_fields(fixture, monkeypatch):
    args, data, *_ = fixture
    response = {**data, "status": "ready", "forward_status": {"state": "ready", "error": "secret-error"},
                "health_status": {"state": "ready"}, "services": [{"Env": "private-env"}]}
    calls = []
    def command(argv, *_):
        calls.append(argv)
        return 0, json.dumps(response).encode()
    monkeypatch.setattr(soak, "read_command", command)
    ok, evidence = soak.sample_status(args, {}, threading.Event())
    assert ok and evidence == {"exit_code": 0, "status": "ready", "forward_state": "ready", "health_state": "ready"}
    assert calls[0][1] == "status" and "--context" in calls[0] and "--namespace" in calls[0]


def resource_fixture(args):
    def resource(name, uid):
        return {"metadata": {"name": name, "uid": uid, "namespace": args.namespace,
                             "labels": {soak.MANAGED: "podgrove", soak.ENVIRONMENT: args.identity}}}
    name = "pg-" + args.identity
    controller = resource(name, "controller-uid")
    pod = resource(name + "-0", "pod-uid")
    pod["metadata"]["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": "StatefulSet", "name": name,
                                          "uid": "controller-uid", "controller": True}]
    pod["spec"] = {"volumes": [{"persistentVolumeClaim": {"claimName": name}}]}
    return {"statefulset": controller, "pod": pod, "persistentvolumeclaim": resource(name, "pvc-uid")}


def test_resource_checks_only_get_three_exact_names_in_explicit_namespace(fixture, monkeypatch):
    args, *_ = fixture
    resources = resource_fixture(args)
    commands = []
    def read(command, *_):
        commands.append(command)
        assert command[:7] == ["kubectl", "--context", args.context, "--namespace", args.namespace, "--request-timeout=30s", "get"]
        assert command[-2:] == ["-o", "json"]
        return 0, json.dumps(resources[command[7]]).encode()
    monkeypatch.setattr(soak, "read_command", read)
    assert soak.owned_uids(args, {}, threading.Event()) == {"statefulset": "controller-uid", "pod": "pod-uid", "pvc": "pvc-uid"}
    assert [(row[7], row[8]) for row in commands] == [
        ("statefulset", "pg-" + args.identity), ("pod", "pg-" + args.identity + "-0"),
        ("persistentvolumeclaim", "pg-" + args.identity)]


@pytest.mark.parametrize("change", ["namespace", "label", "uid", "deleting", "owner", "claim"])
def test_foreign_missing_or_rebound_resources_are_refused(fixture, monkeypatch, change):
    args, *_ = fixture
    resources = resource_fixture(args)
    meta = resources["pod"]["metadata"]
    if change == "namespace":
        meta["namespace"] = "other"
    elif change == "label":
        meta["labels"][soak.ENVIRONMENT] = "other"
    elif change == "uid":
        meta["uid"] = ""
    elif change == "deleting":
        meta["deletionTimestamp"] = "now"
    elif change == "owner":
        meta["ownerReferences"][0]["uid"] = "foreign"
    else:
        resources["pod"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "other"
    monkeypatch.setattr(soak, "read_command", lambda command, *_: (0, json.dumps(resources[command[7]]).encode()))
    with pytest.raises(soak.Refused):
        soak.owned_uids(args, {}, threading.Event())


def cli_arguments(args):
    return [value for key in ("binary", "wheel", "project_directory", "state_home", "kubeconfig", "marker_file", "output",
                              "context", "namespace", "identity", "url")
            for value in ("--" + key.replace("_", "-"), str(getattr(args, key)))]


def test_default_duration_exceeds_four_hours(fixture):
    args, *_ = fixture
    parsed = soak.arguments(cli_arguments(args))
    assert parsed.duration == 14460 and parsed.probe_interval == 10 and parsed.status_interval == 60
    assert parsed.inject_after is None


@pytest.mark.parametrize("key,value", [("binary", "relative/bin/podgrove"), ("namespace", "default;delete"),
    ("identity", "not-an-identity"), ("url", "http://remote.invalid:23456/"),
    ("url", "http://127.0.0.1:23456/?token=secret"), ("url", "http://user:password@127.0.0.1:23456/"),
    ("url", "https://127.0.0.1:23456/"), ("url", "http://127.0.0.1:99999/")])
def test_arguments_refuse_implicit_paths_unsafe_scope_or_external_url(fixture, key, value):
    args, *_ = fixture
    setattr(args, key, value)
    with pytest.raises(SystemExit):
        soak.arguments(cli_arguments(args))


def test_private_reads_refuse_symlinks_hardlinks_and_shared_writable_files(tmp_path):
    source = tmp_path / "source"
    source.write_text("secret")
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(OSError):
        soak.private_read(link)
    link.unlink()
    os.link(source, link)
    with pytest.raises(soak.Refused):
        soak.private_read(source)
    link.unlink()
    source.chmod(0o666)
    with pytest.raises(soak.Refused):
        soak.private_read(source)


def test_unverified_checkout_or_conflicting_receipt_cannot_be_used(fixture, monkeypatch):
    args, *_ = fixture
    with pytest.raises(soak.Refused, match="entry point"):
        soak.binary_proof(args.binary, {}, threading.Event(), wheel=args.wheel)
    python = args.binary.parent / "python"
    args.binary.write_text(f"#!{python}\n" + soak.ENTRYPOINT)
    receipt = args.binary.parents[2] / "installation.json"
    receipt.write_text(json.dumps({"version": "0.2.0", "tag": "v0.2.0", "source_commit": "a" * 40,
                                   "executable": str(args.binary)}))
    read = Mock(side_effect=AssertionError("Conflicting receipt must fail before executing"))
    monkeypatch.setattr(soak, "read_command", read)
    with pytest.raises(soak.Refused, match="receipt"):
        soak.binary_proof(args.binary, {}, threading.Event(), wheel=args.wheel)
    read.assert_not_called()


@pytest.fixture
def wheel_install(tmp_path):
    venv = tmp_path / "0.2.0-aaaaaaaaaaaa/venv"
    package = venv / "lib/python3.12/site-packages/podgrove"
    package.mkdir(parents=True)
    payload = {"__init__.py": '__version__ = "0.2.0"\n', "cli.py": "def main(): return 0\n"}
    wheel = tmp_path / "podgrove-0.2.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, text in payload.items():
            archive.writestr("podgrove/" + name, text)
            (package / name).write_text(text)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    receipt = {"version": "0.2.0", "assets": {wheel.name: digest}}
    return venv, package, wheel, receipt, digest


def test_installed_bytes_are_bound_to_receipt_verified_release_wheel(wheel_install):
    venv, package, wheel, receipt, digest = wheel_install
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "cli.cpython-312.pyc").write_bytes(b"ignored; observer uses a fresh cache prefix")
    assert soak.verify_wheel_payload(venv, wheel, receipt) == digest


@pytest.mark.parametrize("change", ["payload", "extra", "missing", "symlink", "wheel"])
def test_matching_version_cannot_hide_modified_installation(wheel_install, change):
    venv, package, wheel, receipt, _ = wheel_install
    if change == "payload":
        (package / "cli.py").write_text("injected despite same version")
    elif change == "extra":
        (package / "injected.py").write_text("extra code")
    elif change == "missing":
        (package / "cli.py").unlink()
    elif change == "symlink":
        (package / "injected.py").symlink_to(package / "cli.py")
    else:
        wheel.write_bytes(wheel.read_bytes() + b"tampered")
    with pytest.raises(soak.Refused):
        soak.verify_wheel_payload(venv, wheel, receipt)


def test_subprocess_cleanup_kills_inheriting_helper_after_leader_exits(tmp_path):
    pid_file, marker = tmp_path / "pid", tmp_path / "survived"
    script = ("import os,time,pathlib; child=os.fork();\n"
              "if child: os._exit(0)\n"
              f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
              f"time.sleep(.5); pathlib.Path({str(marker)!r}).write_text('leaked')\n")
    try:
        with pytest.raises(soak.Refused, match="timed out"):
            soak.read_command([sys.executable, "-c", script], dict(os.environ), threading.Event(), timeout=.15)
        time.sleep(.55)
        assert pid_file.exists() and not marker.exists()
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_trickled_http_headers_have_total_deadline(monkeypatch):
    monkeypatch.setattr(soak, "HTTP_TIMEOUT", .2)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            try:
                self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                for _ in range(50):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(.02)
            except OSError:
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01})
    thread.start()
    started = time.monotonic()
    try:
        assert not soak.http_probe(soak.urlsplit(f"http://127.0.0.1:{server.server_port}/"), "fixture")
        assert time.monotonic() - started < .8
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_sampling_wall_clock_gap_cannot_be_counted_as_continuous_soak(fixture, monkeypatch):
    args, *_ = mocked_observations(monkeypatch, fixture)
    clock = SimpleNamespace(monotonic=time.monotonic, time=lambda: 100.0)
    monkeypatch.setattr(soak, "time", clock)
    reads = 0
    def probe(*_):
        nonlocal reads
        reads += 1
        if reads == 2:
            clock.time = lambda: 1000.0
        return True
    monkeypatch.setattr(soak, "http_probe", probe)
    assert soak.run(args, threading.Event()) == 1
    result = read_result(args)
    assert not result["passed"] and result["max_http_wall_gap_seconds"] >= 900
    assert result["counts"]["errors"] == 1


def test_unexpected_observer_failure_is_recorded_without_secret_exception_payload(fixture, monkeypatch):
    args, *_ = mocked_observations(monkeypatch, fixture)
    monkeypatch.setattr(soak, "sample_status", Mock(side_effect=RuntimeError("token=never-persist-this")))
    assert soak.run(args, threading.Event()) == 1
    assert read_result(args)["counts"]["errors"] == 1
    text = (args.output / "samples.jsonl").read_text()
    assert "RuntimeError" in text and "never-persist-this" not in text
