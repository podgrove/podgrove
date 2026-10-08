"""Owner-reported startup failures remain visible and machine-readable."""
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from podgrove import cli, runtime, state
from podgrove.errors import PodgroveError
from podgrove.process import run
from podgrove.startup_progress import StartupLog, StartupProgress
from test_live_findings_cli import project as project


def test_build_output_is_delivered_before_the_process_can_complete(tmp_path):
    acknowledged = tmp_path / "acknowledged"
    program = '''
import pathlib, sys, time
ack = pathlib.Path(sys.argv[1])
print("#8 [api 2/5] RUN compile", flush=True)
while not ack.exists():
    time.sleep(.01)
print("#8 DONE 0.1s", file=sys.stderr, flush=True)
'''
    lines = []
    def output(stream, line):
        lines.append((stream, line))
        if "2/5" in line:
            acknowledged.write_text("observed while still building")
    result = run([sys.executable, "-c", program, str(acknowledged)], timeout=5, on_output=output)
    assert result.returncode == 0
    assert lines == [("stdout", "#8 [api 2/5] RUN compile"), ("stderr", "#8 DONE 0.1s")]


def test_streaming_handles_split_unicode_long_lines_and_bounded_capture():
    lines = []
    program = 'import os;os.write(1,b"\\xe2");os.write(1,b"\\x82\\xac\\rnext\\n");os.write(2,b"x"*2097152)'
    result = run([sys.executable, "-c", program], timeout=10, on_output=lambda stream, line: lines.append((stream, line)))
    assert lines[:2] == [("stdout", "€"), ("stdout", "next")]
    assert sum(len(line) for stream, line in lines if stream == "stderr") == 2097152
    assert len(result.stderr) == 1024 * 1024


@pytest.mark.parametrize("cancel", [False, True])
def test_streaming_stops_boundedly_and_reaps_pipe_holding_children(tmp_path, cancel):
    marker = tmp_path / "pid"
    program = '''
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
print("building", flush=True)
time.sleep(60)
'''
    cancelled = threading.Event()
    started = time.monotonic()
    def output(*_):
        if cancel:
            cancelled.set()
    with pytest.raises(PodgroveError, match="cancelled" if cancel else "timed out"):
        run([sys.executable, "-c", program, str(marker)], timeout=.8, cancel_event=cancelled, on_output=output)
    assert time.monotonic() - started < 4
    pid = int(marker.read_text())
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], text=True, capture_output=True)
    assert result.returncode != 0 or result.stdout.strip().startswith("Z")


def test_progress_tracks_only_declared_services_build_steps_and_start_states():
    snapshots = []
    progress = StartupProgress(snapshots.append, time.time() - 7)
    progress.configure({"name": "fixture", "services": {"api": {"environment": {"TOKEN": "private"}}, "off": {"scale": 0}}})
    progress.set_phase("building and starting Compose services")
    progress.output("#8 [api stage 2/5] RUN compile")
    assert snapshots[-1]["services"] == [{"service": "api", "state": "building", "progress": "stage 2/5"}]
    assert snapshots[-1]["elapsed_seconds"] >= 7
    progress.output(" Container fixture-api-1 Starting")
    assert snapshots[-1]["services"] == [{"service": "api", "state": "starting"}]
    progress.observe([{"Service": "api", "State": "running", "Health": "starting"}])
    assert snapshots[-1]["services"][0]["state"] == "running/starting"
    progress.output("#9 [other 1/4] secret")
    assert "private" not in json.dumps(snapshots) and "secret" not in json.dumps(snapshots)


def test_single_service_build_labels_without_a_service_prefix_are_identified():
    snapshots = []
    progress = StartupProgress(snapshots.append)
    progress.configure({"services": {"api": {"build": {"context": "."}}, "database": {"image": "postgres"}}})
    progress.output("#5 [stage-0 3/6] RUN install")
    assert snapshots[-1]["services"][0] == {"service": "api", "state": "building", "progress": "stage-0 3/6"}


@pytest.mark.parametrize("event", ["Building", "Built", "Pulling", "Pulled", "Error"])
def test_image_progress_maps_to_declared_services_only(event):
    snapshots = []
    progress = StartupProgress(snapshots.append)
    progress.configure({"name": "fixture", "services": {"api": {"image": "custom/image:1"},
                       "worker": {"image": "custom/image:1"}, "other": {"image": "different:1"}}})
    progress.output(f" Image custom/image:1 {event} ")
    assert [service["state"] for service in snapshots[-1]["services"]] == [event.lower(), event.lower(), "pending"]
    progress.output(" Image undeclared/image:1 Building ")
    assert [service["state"] for service in snapshots[-1]["services"]] == [event.lower(), event.lower(), "pending"]


@pytest.mark.parametrize("header", [False, True])
def test_classic_builder_steps_identify_a_single_declared_builder(header):
    snapshots = []
    progress = StartupProgress(snapshots.append)
    progress.configure({"name": "fixture", "services": {"api": {"build": {"context": "."}}, "db": {"image": "postgres"}}})
    if header:
        progress.output(" Image fixture-api Building ")
    progress.output("Step 2/3 : RUN compile")
    assert snapshots[-1]["services"][0] == {"service": "api", "state": "building", "progress": "step 2/3"}


def test_classic_builder_does_not_guess_between_concurrent_or_shared_images():
    snapshots = []
    progress = StartupProgress(snapshots.append)
    progress.configure({"name": "fixture", "services": {name: {"build": {"context": "."}} for name in ("api", "worker")}})
    progress.output("Step 2/3 : RUN compile")
    assert all(service["state"] == "pending" for service in snapshots[-1]["services"])
    progress.output(" Image fixture-api Building ")
    progress.output("Step 2/3 : RUN compile")
    assert snapshots[-1]["services"][0]["progress"] == "step 2/3"
    progress.output(" Image fixture-worker Building ")
    before = snapshots[-1]
    progress.output("Step 3/4 : RUN ambiguous")
    assert snapshots[-1] == before
    progress.output(" Image fixture-api Built ")
    progress.output("Step 3/4 : RUN worker")
    assert snapshots[-1]["services"][1]["progress"] == "step 3/4"
    progress.output(" Image undeclared:1 Building ")
    before = snapshots[-1]
    progress.output("Step 4/5 : RUN ambiguous")
    assert snapshots[-1] == before
    progress.configure({"services": {name: {"image": "shared:1", "build": {"context": "."}} for name in ("api", "worker")}})
    progress.output(" Image shared:1 Building ")
    before = snapshots[-1]
    progress.output("Step 2/3 : RUN ambiguous")
    assert snapshots[-1] == before


def test_partial_diagnostics_include_each_failed_service_and_missing_replica(monkeypatch, tmp_path):
    model = {"services": {"ready": {}, "dead": {}, "unhealthy": {}, "missing": {}, "scaled": {"scale": 0},
                          "job": {}, "consumer": {"depends_on": {"job": {"condition": "service_completed_successfully"}}}}}
    rows = [{"Service": "ready", "State": "running", "Health": "healthy"},
            {"Service": "dead", "State": "exited", "ExitCode": 7, "ID": "dead-id"},
            {"Service": "unhealthy", "State": "running", "Health": "unhealthy", "ID": "unhealthy-id"},
            {"Service": "job", "State": "exited", "ExitCode": 0},
            {"Service": "consumer", "State": "created", "ID": "consumer-id"}]
    compose = SimpleNamespace(config=SimpleNamespace(root=tmp_path), command=lambda *args: ["docker", "compose", *args])
    calls = []
    def read(command, **kwargs):
        calls.append(command)
        assert 0 < kwargs["timeout"] <= 5
        if command[1] == "inspect":
            return SimpleNamespace(stdout=json.dumps({"restart_count": 3, "exit_code": 7 if command[-1] == "dead-id" else 0}))
        return SimpleNamespace(stdout="\n".join(f"{command[-1]} line {number}" for number in range(50)))
    monkeypatch.setattr(runtime, "run", read)
    result = runtime.startup_diagnostics(compose, model, {}, rows)
    assert [item["service"] for item in result] == ["dead", "unhealthy", "missing", "consumer"]
    assert result[0]["containers"][0]["restart_count"] == 3
    assert result[0]["containers"][0]["exit_code"] == 7
    assert result[2]["containers"][0]["state"] == "not created"
    assert all(len(item["logs"]) == 40 and item["logs"][0].endswith("line 10") for item in result)
    assert all(command[2:6] == ["logs", "--no-color", "--tail", "40"] for command in calls if command[1] == "compose")


def test_starting_status_returns_success_with_elapsed_phase_and_service_state(project, capsys):
    project.record(status="starting", created_at=time.time() - 10, startup_progress={
        "phase": "building and starting Compose services", "started_at": time.time() - 10,
        "services": [{"service": "app", "state": "building", "progress": "2/5"}]})
    assert cli.execute(cli.parser().parse_args(["status", "--json"])) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["startup_progress"]["elapsed_seconds"] >= 10
    assert data["startup_progress"]["services"][0]["progress"] == "2/5"
    assert cli.execute(cli.parser().parse_args(["status"])) == 0
    output = capsys.readouterr().out
    assert "building and starting" in output and "10" in output and "app: building — 2/5" in output


@pytest.mark.parametrize("elapsed", [1, 100000])
def test_starting_status_refuses_an_unreachable_recorded_supervisor(project, capsys, elapsed):
    original = project.record(status="starting", pid=1234, created_at=time.time() - elapsed,
                              startup_progress={"phase": "building Compose services"})
    project.running.return_value = False
    assert cli.execute(cli.parser().parse_args(["status", "--json"])) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "error"
    assert "Startup supervisor is unreachable during building Compose services" in data["error"]
    assert "up to reconnect" in data["error"]
    assert state.read(project.path) == original


def test_authenticated_startup_remains_progressing_during_a_long_build(project, capsys):
    project.record(status="starting", pid=1234, created_at=time.time() - 100000, timeout=60,
                   startup_progress={"phase": "building Compose services", "started_at": time.time() - 100000})
    assert cli.execute(cli.parser().parse_args(["status", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "starting"


@pytest.mark.parametrize("elapsed,expected", [(1, 0), (59, 0), (61, 1)])
def test_pre_supervisor_startup_grace_is_bounded_by_the_recorded_timeout(project, capsys, elapsed, expected):
    original = project.record(status="starting", created_at=time.time() - elapsed, timeout=60)
    project.running.return_value = False
    assert cli.execute(cli.parser().parse_args(["status", "--json"])) == expected
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == ("starting" if expected == 0 else "error")
    if expected:
        assert "within the provisioning timeout" in data["error"]
    assert state.read(project.path) == original


def test_starting_status_without_a_supervisor_or_start_time_is_not_success(project, capsys):
    project.record(status="starting")
    project.running.return_value = False
    assert cli.execute(cli.parser().parse_args(["status", "--json"])) == 1
    assert "provisioning timeout" in json.loads(capsys.readouterr().out)["error"]


def test_fresh_healthy_status_discards_cached_startup_failure(project, monkeypatch, capsys):
    original = project.record(status="unhealthy", error="Old startup failure",
                              startup_status={"state": "failed", "error": "Old startup failure"},
                              startup_diagnostics=[{"service": "app", "containers": [], "logs": ["Old traceback"]}])
    project.ping.update(status="unhealthy", startup_status=original["startup_status"])
    monkeypatch.setattr(runtime, "service_status", lambda *_: [{"Service": "app", "State": "running", "Health": "healthy"}])
    assert cli.execute(cli.parser().parse_args(["status", "--json"])) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready" and result["startup_status"]["state"] == "ready"
    assert "startup_diagnostics" not in result and "error" not in result
    assert cli.execute(cli.parser().parse_args(["status"])) == 0
    output = capsys.readouterr().out
    assert "Old traceback" not in output and "Old startup failure" not in output
    assert "Fix the service errors" not in output
    assert state.read(project.path) == original


def test_partial_up_prints_cause_restart_count_and_logs_without_losing_json(project, capsys):
    def partial(path):
        saved = state.read(path)
        saved.update(status="unhealthy", error="dependency app failed to start", startup_status={"state": "failed"},
                     startup_diagnostics=[{"service": "app", "containers": [{"state": "exited", "exit_code": 7, "restart_count": 3}],
                                           "logs": ["FileNotFoundError: /keys/private.pem"]}])
        state.write(path, saved)
    project.spawn.side_effect = partial
    assert cli.execute(cli.parser().parse_args(["up", "--json"])) == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["startup_diagnostics"][0]["logs"] == ["FileNotFoundError: /keys/private.pem"]
    cli.print_state(parsed)
    output = capsys.readouterr().out
    assert "app" in output and "exit=7; restarts=3" in output and "/keys/private.pem" in output
    assert "run podgrove up again" in output


def test_current_launch_log_follows_only_new_output_and_preserves_partial_lines(tmp_path):
    path = tmp_path / "session.log"
    path.write_text("previous launch\n")
    log = StartupLog(path)
    with path.open("a") as stream:
        stream.write("Startup: build\n#8 [api")
    assert log.read() == "Startup: build\n"
    with path.open("a") as stream:
        stream.write(" 2/5] RUN compile\n")
    assert log.read() == "#8 [api 2/5] RUN compile\n"
    assert log.read() == ""


def test_startup_log_handles_unicode_split_across_polls_and_large_final_backlog(tmp_path):
    path = tmp_path / "session.log"
    log = StartupLog(path)
    path.write_bytes(b"\xe2")
    assert log.read() == ""
    with path.open("ab") as stream:
        stream.write(b"\x82\xac\n" + b"build output\n" * 100000)
    output = []
    while True:
        output.append(log.read(final=True))
        if not log.has_more:
            break
    assert "".join(output) == "€\n" + "build output\n" * 100000


def test_up_streams_build_progress_before_ready_and_keeps_json_stdout_clean(project, monkeypatch, capsys):
    log = project.path.with_suffix(".log")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("previous launch\n")
    def launch(path):
        with log.open("a") as stream:
            stream.write("#8 [app 2/5] RUN compile\n")
    project.spawn.side_effect = launch
    def advance(_):
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "#8 [app 2/5] RUN compile\n"
        saved = state.read(project.path)
        saved["status"] = "ready"
        state.write(project.path, saved)
    monkeypatch.setattr(cli.time, "sleep", advance)
    assert cli.execute(cli.parser().parse_args(["up", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_diagnostics_keep_all_failures_when_docker_observation_fails(monkeypatch, tmp_path):
    model = {"services": {"api": {}, "worker": {}}}
    compose = SimpleNamespace(config=SimpleNamespace(root=tmp_path), command=lambda *args: ["docker", "compose", *args])
    def unavailable(*_, **__):
        raise PodgroveError("Docker observation unavailable")
    monkeypatch.setattr(runtime, "run", unavailable)
    reports = runtime.startup_diagnostics(compose, model, {}, [{"Service": "api", "State": "exited", "ID": "api-id"}])
    assert [item["service"] for item in reports] == ["api", "worker"]
    assert all(item["logs_error"] == "Docker observation unavailable" for item in reports)
    assert reports[0]["containers"][0]["observation_error"] == "Docker observation unavailable"
