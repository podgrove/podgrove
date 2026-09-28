"""Offline safety and scenario regressions for explicit startup live acceptance."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location("check_startup_recovery", Path(__file__).resolve().parents[1] / "scripts/check_startup_recovery.py")
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)
IDENT = "012345abcdef"
NS = "acceptance-tests"


def objects():
    name = "pg-" + IDENT
    values = []
    for kind in ("StatefulSet", "Pod", "PersistentVolumeClaim", "PodDisruptionBudget"):
        meta = {"name": name + ("-0" if kind == "Pod" else ""), "namespace": NS, "uid": kind + "-uid", "resourceVersion": "1",
                "labels": {acceptance.base.MANAGED: "podgrove", acceptance.base.ENVIRONMENT: IDENT}}
        value = {"kind": kind, "metadata": meta}
        if kind == "Pod":
            meta["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": "StatefulSet", "name": name,
                                        "uid": "StatefulSet-uid", "controller": True}]
            meta["annotations"] = dict(zip(acceptance.ANNOTATIONS, ("false", "true")))
            value["spec"] = {"volumes": [{"persistentVolumeClaim": {"claimName": name}}]}
        if kind == "StatefulSet":
            value["spec"] = {"template": {"metadata": {"annotations": dict(zip(acceptance.ANNOTATIONS, ("false", "true")))}}}
        values.append(value)
    return {"items": values}


@pytest.fixture
def setup(tmp_path):
    args = SimpleNamespace(output=tmp_path / "evidence", podgrove_bin=Path(sys.executable), context="explicit-context",
                           namespace=NS, storage_class="approved-storage")
    args.output.mkdir()
    runner = acceptance.Runner(args)
    root, state = tmp_path / "fixture", tmp_path / "state"
    (root / "content").mkdir(parents=True)
    state.mkdir()
    role = {"role": "ordinary", "identity": IDENT, "root": root, "state": state, "attempted": True,
            "captured": acceptance.base.checked_objects(objects(), NS, IDENT)}
    return runner, role


def test_plan_only_never_creates_output_or_runs_a_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(acceptance.Runner, "run", Mock(side_effect=AssertionError("must not execute")))
    output = tmp_path / "unused"
    assert acceptance.main(["--podgrove-bin", "/not/an/executable", "--context", "explicit", "--namespace", NS,
                            "--storage-class", "test-storage", "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["execute"] is False
    assert not output.exists()


@pytest.mark.parametrize("option,value", [("--podgrove-bin", "relative"), ("--namespace", "kube-system"),
                                          ("--context", ""), ("--storage-class", ""), ("--output", "relative")])
def test_execute_refuses_incomplete_or_unsafe_targets(tmp_path, option, value, monkeypatch):
    monkeypatch.setattr(acceptance.Runner, "run", Mock(side_effect=AssertionError("must not execute")))
    args = {"--podgrove-bin": sys.executable, "--context": "explicit", "--namespace": NS,
            "--storage-class": "test-storage", "--output": str(tmp_path / "new")}
    args[option] = value
    with pytest.raises(SystemExit) as error:
        acceptance.main([item for pair in args.items() for item in pair] + ["--execute"])
    assert error.value.code == 2


@pytest.mark.parametrize("kind", ["Pod", "StatefulSet"])
@pytest.mark.parametrize("key", acceptance.ANNOTATIONS)
def test_each_annotation_mutation_is_guarded_and_removes_only_that_key(kind, key):
    resource = next(item for item in objects()["items"] if item["kind"] == kind)
    patch = acceptance.annotation_patch(resource, key)
    assert patch[:2] == [{"op": "test", "path": "/metadata/uid", "value": kind + "-uid"},
                         {"op": "test", "path": "/metadata/resourceVersion", "value": "1"}]
    assert patch[-1]["op"] == "remove" and patch[-1]["path"] == patch[-2]["path"]
    assert len(patch) == 4 and patch[-2]["op"] == "test"
    target = resource["metadata"] if kind == "Pod" else resource["spec"]["template"]["metadata"]
    del target["annotations"][key]
    with pytest.raises(acceptance.AcceptanceError, match="absent"):
        acceptance.annotation_patch(resource, key)


@pytest.mark.parametrize("mutation", ["controller", "pvc", "pod-owner", "pod-volume", "namespace", "unplanned-pod"])
def test_replacement_exception_cannot_adopt_foreign_storage_or_controller(setup, mutation):
    _runner, role = setup
    payload = objects()
    role["replacement_pending"] = {"Pod/pg-" + IDENT + "-0"}
    payload["items"][1]["metadata"]["uid"] = "replacement-pod"
    if mutation in ("controller", "pvc"):
        payload["items"][0 if mutation == "controller" else 2]["metadata"]["uid"] = "foreign"
    elif mutation == "pod-owner":
        payload["items"][1]["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    elif mutation == "pod-volume":
        payload["items"][1]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "another-volume"
    elif mutation == "namespace":
        payload["items"][1]["metadata"]["namespace"] = "another-namespace"
    else:
        role["replacement_pending"].clear()
    with pytest.raises(acceptance.AcceptanceError):
        acceptance.prove_inventory(payload, role, NS)


def test_only_explicit_replacement_is_permitted_and_final_empty_inventory_is_valid(setup):
    _runner, role = setup
    role["replacement_pending"] = {"Pod/pg-" + IDENT + "-0"}
    payload = objects()
    payload["items"][1]["metadata"]["uid"] = "replacement-pod"
    proof = acceptance.prove_inventory(payload, role, NS)
    assert proof["Pod/pg-" + IDENT + "-0"] == "replacement-pod"
    assert acceptance.prove_inventory({"items": []}, role, NS) == {}


@pytest.mark.parametrize("kind", ["Pod", "PodDisruptionBudget"])
def test_raw_delete_has_exact_scope_uid_resourceversion_and_never_force(setup, monkeypatch, kind):
    runner, role = setup
    resource = next(item for item in objects()["items"] if item["kind"] == kind)
    monkeypatch.setattr(runner, "object", Mock(return_value=deepcopy(resource)))
    command = Mock(return_value=("{}", 0))
    monkeypatch.setattr(runner, "command", command)
    runner.delete_exact(role, resource)
    argv = command.call_args.args[0]
    assert argv[:5] == ["kubectl", "--context", "explicit-context", "--namespace", NS]
    assert "--force" not in argv and "--all" not in argv and "--all-namespaces" not in argv
    rawpath = argv[argv.index("--raw") + 1]
    prefix, plural = ("/api/v1", "pods") if kind == "Pod" else ("/apis/policy/v1", "poddisruptionbudgets")
    assert rawpath == f"{prefix}/namespaces/{NS}/{plural}/{resource['metadata']['name']}"
    path = Path(argv[argv.index("-f") + 1])
    options = json.loads(path.read_text())
    assert options["preconditions"] == {"uid": kind + "-uid", "resourceVersion": "1"}
    assert options.get("gracePeriodSeconds") == (30 if kind == "Pod" else None)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("field", ["uid", "resourceVersion", "name", "namespace"])
def test_mutation_rechecks_target_immediately_before_delete(setup, monkeypatch, field):
    runner, role = setup
    resource = objects()["items"][1]
    current = deepcopy(resource)
    current["metadata"][field] = "changed"
    monkeypatch.setattr(runner, "object", lambda *_: current)
    command = Mock()
    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(acceptance.AcceptanceError, match="changed"):
        runner.delete_exact(role, resource)
    command.assert_not_called()


def test_midbuild_probe_is_read_only_uid_guarded_and_requires_exact_process_evidence(setup, monkeypatch):
    runner, role = setup
    command = Mock(return_value=("/proc/123/cmdline\n", 0))
    monkeypatch.setattr(runner, "command", command)
    assert runner.build_is_running(role, "captured-pod", "a" * 32)
    argv = command.call_args.args[0]
    assert "captured-pod" in argv and acceptance.UID_GUARD in argv and acceptance.PROCESS_PROBE in argv
    assert argv[-2:] == [acceptance.BUILD_PROGRAM, "a" * 32]
    assert not any(arg in argv for arg in ("delete", "patch", "node", "namespace"))
    for value in (("", 0), ("/proc/123/cmdline\n", 126), ("unrelated output", 0)):
        command.return_value = value
        assert not runner.build_is_running(role, "captured-pod", "a" * 32)


def test_actual_outer_guard_refuses_replaced_pod_without_running_child(tmp_path):
    target = tmp_path / "must-not-exist"
    result = subprocess.run(["sh", "-c", acceptance.UID_GUARD, "test-guard", "expected", sys.executable, "-c",
                             "from pathlib import Path;import sys;Path(sys.argv[1]).touch()", str(target)],
                            env={"PODGROVE_POD_UID": "replacement"}, capture_output=True, timeout=3)
    assert result.returncode == 126 and not target.exists()


def test_serial_coordinator_finishes_cleanup_before_next_fixture(setup, monkeypatch):
    runner, role = setup
    runner.fixtures = [{**role, "role": name} for name in ("ordinary", "refresh", "midbuild")]
    events = []
    monkeypatch.setattr(runner, "partial_case", lambda item, **kw: events.append((item["role"], "refresh" if kw["refresh"] else "up")))
    monkeypatch.setattr(runner, "protection_case", lambda item: events.append((item["role"], "protection")))
    monkeypatch.setattr(runner, "midbuild_case", lambda item: events.append((item["role"], "midbuild")))
    def cleanup(item):
        events.append((item["role"], "cleanup"))
        item["cleaned"] = True
    monkeypatch.setattr(runner, "cleanup_role", cleanup)
    runner.exercise()
    assert events == [("ordinary", "up"), ("ordinary", "protection"), ("ordinary", "cleanup"),
                      ("refresh", "refresh"), ("refresh", "cleanup"), ("midbuild", "midbuild"), ("midbuild", "cleanup")]


def test_serial_cleanup_failure_prevents_any_new_engine(setup, monkeypatch):
    runner, role = setup
    runner.fixtures = [{**role, "role": name} for name in ("ordinary", "refresh", "midbuild")]
    started = []
    monkeypatch.setattr(runner, "partial_case", lambda item, **_: started.append(item["role"]))
    monkeypatch.setattr(runner, "protection_case", lambda *_: None)
    monkeypatch.setattr(runner, "cleanup_role", lambda item: item.update(cleaned=False))
    with pytest.raises(acceptance.AcceptanceError, match="refusing to start"):
        runner.exercise()
    assert started == ["ordinary"]


def test_partial_scenario_requires_failed_recreation_and_preserves_healthy_identity(setup, monkeypatch):
    runner, role = setup
    before = {"services": [{"Service": name, "ID": str(number) * 12,
                           "State": "exited" if name == "exited" else "running",
                           "Health": "unhealthy" if name == "unhealthy" else "healthy"}
                          for number, name in enumerate(("healthy", "exited", "unhealthy"), 1)],
              "ports": [{"service": "healthy"}]}
    after = deepcopy(before)
    for index in (1, 2):
        after["services"][index]["ID"] = str(index + 3) * 12
    up = Mock(side_effect=[before, after])
    monkeypatch.setattr(runner, "run_up", up)
    monkeypatch.setattr(runner, "local_http", Mock())
    monkeypatch.setattr(runner, "cli", Mock(return_value=("diagnostic-ready\n", 0)))
    runner.partial_case(role, refresh=True)
    assert (role["root"] / "content/required.txt").read_text() == "fixture recovery\n"
    assert up.call_args_list[1].kwargs == {"refresh": True}
    assert runner.result["checks"]["ordinary"]["before_ids"]["healthy"] == runner.result["checks"]["ordinary"]["after_ids"]["healthy"]


def test_cleanup_role_never_removes_shared_fixture_base_early(setup, monkeypatch):
    runner, role = setup
    other = {**role, "role": "other"}
    runner.fixtures, runner.base = [role, other], role["root"].parent
    original = runner.base
    def cleanup(active):
        assert active.fixtures == [role] and active.base is None
        active.result["cleanup"][role["role"]] = {"passed": True}
    monkeypatch.setattr(acceptance.base.Runner, "cleanup", cleanup)
    runner.cleanup_role(role)
    assert runner.base == original and runner.fixtures == [role, other] and original.is_dir()
    assert role["cleaned"]


def test_protection_doctor_must_return_json_error_not_unrelated_failure(setup, monkeypatch):
    runner, role = setup
    cli = Mock(return_value=(json.dumps({"command": "doctor", "status": "error",
                                       "error": "Existing engine eviction protection is missing or changed; run up"}), 1))
    monkeypatch.setattr(runner, "cli", cli)
    runner.doctor_refuses(role)
    for payload, code in (({"command": "doctor", "status": "error"}, 0), ({"status": "error"}, 1), ({"command": "doctor", "status": "passed"}, 1)):
        cli.return_value = json.dumps(payload), code
        with pytest.raises(acceptance.AcceptanceError):
            runner.doctor_refuses(role)


@pytest.mark.parametrize("error", [ProcessLookupError, PermissionError])
def test_child_exit_race_does_not_replace_primary_acceptance_failure(monkeypatch, error):
    process = Mock(pid=42)
    process.poll.side_effect = [None, 0]
    monkeypatch.setattr(acceptance.os, "killpg", Mock(side_effect=error()))
    acceptance.stop_child(process)
    process.wait.assert_called_once_with(timeout=3)


def test_live_child_permission_denial_is_not_suppressed(monkeypatch):
    process = Mock(pid=42)
    process.poll.return_value = None
    monkeypatch.setattr(acceptance.os, "killpg", Mock(side_effect=PermissionError()))
    with pytest.raises(PermissionError):
        acceptance.stop_child(process)


def test_stop_child_reaps_an_actual_owned_process_group():
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time;time.sleep(30)"], start_new_session=True)
    try:
        acceptance.stop_child(process)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_midbuild_coordinator_requires_process_proof_before_one_delete_and_preserves_anchors(setup, monkeypatch):
    runner, role = setup
    role["role"] = "midbuild"
    acceptance.base.write_json(role["root"] / "podgrove.yml", {"compose": {"files": ["compose.yml"]}})
    (role["state"] / "session.log").write_text("Startup: building and starting Compose services\n")
    events = []

    class Process:
        pid = 42
        returncode = None
        def poll(self):
            return self.returncode
        def wait(self, **_):
            self.returncode = 0
            events.append("wait")

    monkeypatch.setattr(acceptance.subprocess, "Popen", lambda *_a, **_kw: Process())
    monkeypatch.setattr(runner, "object", lambda *_: objects()["items"][1])
    def probe(*_):
        events.append("process-proof")
        return True
    monkeypatch.setattr(runner, "build_is_running", probe)
    def observe(*_):
        events.append("observe")
        return {"status": "ready", "startup_status": {"attempts": 1}}
    monkeypatch.setattr(runner, "observe", observe)
    def delete(item, _pod):
        events.append("delete")
        item["replacement_pending"] = {"Pod/pg-" + IDENT + "-0"}
    monkeypatch.setattr(runner, "delete_exact", delete)
    def capture(item):
        events.append("capture")
        item["captured"]["Pod/pg-" + IDENT + "-0"] = "replacement-pod"
    monkeypatch.setattr(runner, "capture", capture)
    monkeypatch.setattr(runner, "local_http", lambda *_: events.append("http"))
    runner.midbuild_case(role)
    assert events == ["process-proof", "observe", "delete", "wait", "observe", "capture", "http"]
    proof = runner.result["checks"]["midbuild"]
    for kind in ("StatefulSet", "PersistentVolumeClaim"):
        assert proof["before"][kind + "/pg-" + IDENT] == proof["after"][kind + "/pg-" + IDENT]
    assert proof["actual_build_observed"] and not role["replacement_pending"]


def test_midbuild_never_deletes_after_up_has_already_exited(setup, monkeypatch):
    runner, role = setup
    acceptance.base.write_json(role["root"] / "podgrove.yml", {"compose": {"files": ["compose.yml"]}})
    process = Mock(returncode=1)
    process.poll.return_value = 1
    monkeypatch.setattr(acceptance.subprocess, "Popen", lambda *_a, **_kw: process)
    delete = Mock()
    monkeypatch.setattr(runner, "delete_exact", delete)
    with pytest.raises(acceptance.AcceptanceError, match="provably running build"):
        runner.midbuild_case(role)
    delete.assert_not_called()
