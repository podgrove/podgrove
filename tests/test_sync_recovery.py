"""Ownership recovery uses bounded read-only observations, never mutations."""
from copy import deepcopy
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from podgrove import kube as kube_module, sync_recovery
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube
from podgrove.process import run
from podgrove.sync_recovery import SyncOwnershipError, SyncRecoveryUnavailable, verify_engine

IDENT = "012345abcdef"
EXPECTED = {"statefulset_uid": "original-controller", "pod_uid": "original-pod"}


class ReadOnlyKube:
    namespace = "owned-test"

    def __init__(self):
        labels = {MANAGED: "podgrove", ENVIRONMENT: IDENT}
        self.controller = {"metadata": {"name": f"pg-{IDENT}", "namespace": self.namespace,
                                       "uid": EXPECTED["statefulset_uid"], "labels": dict(labels)}}
        self.pod = {"metadata": {"name": f"pg-{IDENT}-0", "namespace": self.namespace,
                                "uid": EXPECTED["pod_uid"], "labels": dict(labels),
                                "ownerReferences": [{"apiVersion": "apps/v1", "kind": "StatefulSet",
                                                     "name": f"pg-{IDENT}", "controller": True,
                                                     "uid": EXPECTED["statefulset_uid"]}]}}
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        assert args[0] == "get" and args[1] in ("statefulset", "pod")
        return subprocess.CompletedProcess(args, 0, json.dumps(
            self.controller if args[1] == "statefulset" else self.pod), "")


def test_ownership_proof_reads_only_named_namespaced_resources_with_one_deadline(monkeypatch):
    kube, cancelled = ReadOnlyKube(), threading.Event()
    now = [100.0]
    monkeypatch.setattr(sync_recovery, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = kube.call

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] += 5
        return result

    monkeypatch.setattr(kube, "call", delayed)
    result = verify_engine(kube, IDENT, EXPECTED, cancelled)
    assert result == EXPECTED and result is not EXPECTED
    assert [args for args, _ in kube.calls] == [
        ("get", "statefulset", f"pg-{IDENT}", "-o", "json", "--ignore-not-found", "--request-timeout=14.500000s"),
        ("get", "pod", f"pg-{IDENT}-0", "-o", "json", "--ignore-not-found", "--request-timeout=9.500000s")]
    assert [kwargs["timeout"] for _, kwargs in kube.calls] == [15, 10]
    assert all(kwargs["cancel_event"] is cancelled and kwargs["check"] is False for _, kwargs in kube.calls)


def test_real_kube_command_keeps_explicit_context_namespace_and_exact_names(monkeypatch):
    fixture, cancelled = ReadOnlyKube(), threading.Event()
    commands = []

    def read(command, **kwargs):
        commands.append(command)
        assert kwargs["cancel_event"] is cancelled
        kind = command[command.index("get") + 1]
        value = fixture.controller if kind == "statefulset" else fixture.pod
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    monkeypatch.setattr(kube_module, "run", read)
    verify_engine(Kube("selected-context", fixture.namespace), IDENT, EXPECTED, cancelled)
    assert len(commands) == 2
    for command, kind, name in zip(commands, ("statefulset", "pod"), (f"pg-{IDENT}", f"pg-{IDENT}-0")):
        assert command[:5] == ["kubectl", "--context", "selected-context", "--namespace", fixture.namespace]
        assert command[6:-1] == ["get", kind, name, "-o", "json", "--ignore-not-found"]
        assert command[-1].startswith("--request-timeout=")


@pytest.mark.parametrize("timeout,budgets", [(4, [4, 3]), (100, [15, 14])])
def test_caller_remaining_budget_caps_both_reads_without_extending_fifteen_seconds(monkeypatch, timeout, budgets):
    kube = ReadOnlyKube()
    now = [100.0]
    monkeypatch.setattr(sync_recovery, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = kube.call

    def elapsed(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] += 1
        return result

    monkeypatch.setattr(kube, "call", elapsed)
    assert verify_engine(kube, IDENT, EXPECTED, threading.Event(), timeout=timeout) == EXPECTED
    assert [kwargs["timeout"] for _, kwargs in kube.calls] == budgets


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), None, True, "15"])
def test_invalid_remaining_budget_fails_before_any_read(timeout):
    kube = ReadOnlyKube()
    with pytest.raises(SyncRecoveryUnavailable):
        verify_engine(kube, IDENT, EXPECTED, threading.Event(), timeout=timeout)
    assert not kube.calls


def test_second_read_cannot_return_success_after_shorter_caller_budget(monkeypatch):
    kube = ReadOnlyKube()
    now = [100.0]
    monkeypatch.setattr(sync_recovery, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = kube.call

    def elapsed(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] += 4 if len(kube.calls) == 1 else 2
        return result

    monkeypatch.setattr(kube, "call", elapsed)
    with pytest.raises(SyncRecoveryUnavailable, match="timed out"):
        verify_engine(kube, IDENT, EXPECTED, threading.Event(), timeout=5)
    assert [kwargs["timeout"] for _, kwargs in kube.calls] == [5, 1]


@pytest.mark.parametrize("expected", [None, {}, [], {"statefulset_uid": "x"},
                                     {"statefulset_uid": "x", "pod_uid": ""},
                                     {"statefulset_uid": 1, "pod_uid": "x"},
                                     {"statefulset_uid": "x", "pod_uid": " "}])
def test_missing_original_identity_is_refused_before_any_read(expected):
    kube = ReadOnlyKube()
    with pytest.raises(SyncOwnershipError):
        verify_engine(kube, IDENT, expected, threading.Event())
    assert not kube.calls


@pytest.mark.parametrize("ident", ["", "--all", "../other", "012345abcdef0", None])
def test_invalid_identity_never_becomes_a_kubectl_argument(ident):
    kube = ReadOnlyKube()
    with pytest.raises(SyncOwnershipError):
        verify_engine(kube, ident, EXPECTED, threading.Event())
    assert not kube.calls


@pytest.mark.parametrize("kind", ["controller", "pod"])
@pytest.mark.parametrize("fault", ["missing", "metadata", "labels", "name", "namespace", "uid",
                                   "deleting", "manager", "environment"])
def test_confirmed_ownership_failure_is_not_a_retryable_read(kind, fault):
    kube = ReadOnlyKube()
    value = getattr(kube, kind)
    if fault == "missing":
        value.clear()
    elif fault == "metadata":
        value["metadata"] = []
    elif fault == "labels":
        value["metadata"]["labels"] = []
    elif fault in ("name", "namespace", "uid"):
        value["metadata"][fault] = "replacement"
    elif fault == "deleting":
        value["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    else:
        value["metadata"]["labels"][MANAGED if fault == "manager" else ENVIRONMENT] = "foreign"
    with pytest.raises(SyncOwnershipError):
        verify_engine(kube, IDENT, EXPECTED, threading.Event())
    assert len(kube.calls) == (1 if kind == "controller" else 2)


@pytest.mark.parametrize("fault", ["missing", "not-list", "not-dict", "duplicate", "apiVersion",
                                   "kind", "name", "uid", "controller"])
def test_controller_reference_must_match_the_single_original_controller(fault):
    kube = ReadOnlyKube()
    metadata = kube.pod["metadata"]
    if fault == "missing":
        metadata.pop("ownerReferences")
    elif fault == "not-list":
        metadata["ownerReferences"] = {}
    elif fault == "not-dict":
        metadata["ownerReferences"] = [None]
    elif fault == "duplicate":
        metadata["ownerReferences"] *= 2
    else:
        metadata["ownerReferences"][0][fault] = "foreign" if fault != "controller" else 1
    with pytest.raises(SyncOwnershipError):
        verify_engine(kube, IDENT, EXPECTED, threading.Event())


@pytest.mark.parametrize("failure", [PodgroveError("private diagnostic"), OSError("private diagnostic"),
                                     subprocess.TimeoutExpired("private argument", 15), "exit", "json"])
def test_transport_or_incomplete_response_is_unavailable_without_automatic_retry(monkeypatch, failure):
    kube = ReadOnlyKube()
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args)
        if isinstance(failure, Exception):
            raise failure
        return subprocess.CompletedProcess(args, 1 if failure == "exit" else 0,
                                           "{" if failure == "json" else "", "private diagnostic")

    monkeypatch.setattr(kube, "call", unavailable)
    with pytest.raises(SyncRecoveryUnavailable) as error:
        verify_engine(kube, IDENT, EXPECTED, threading.Event())
    assert len(calls) == 1 and "private" not in str(error.value)


@pytest.mark.parametrize("response", ["", "null", "[]", "{}", "42"])
def test_successful_missing_or_malformed_resource_is_confirmed_failure(monkeypatch, response):
    kube = ReadOnlyKube()
    monkeypatch.setattr(kube, "call", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, response, ""))
    with pytest.raises(SyncOwnershipError):
        verify_engine(kube, IDENT, EXPECTED, threading.Event())


@pytest.mark.parametrize("stop_after", [0, 1, 2])
def test_cancellation_prevents_next_read_or_returning_a_stale_success(monkeypatch, stop_after):
    kube, cancelled = ReadOnlyKube(), threading.Event()
    original = kube.call
    if stop_after == 0:
        cancelled.set()

    def cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        if len(kube.calls) == stop_after:
            cancelled.set()
        return result

    monkeypatch.setattr(kube, "call", cancel)
    with pytest.raises(SyncRecoveryUnavailable, match="cancelled"):
        verify_engine(kube, IDENT, EXPECTED, cancelled)
    assert len(kube.calls) == stop_after


@pytest.mark.parametrize("expire_after", [1, 2])
def test_combined_deadline_rejects_late_success_and_never_resets_for_second_read(monkeypatch, expire_after):
    kube = ReadOnlyKube()
    now = [100.0]
    monkeypatch.setattr(sync_recovery, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = kube.call

    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] += 15 if len(kube.calls) == expire_after else 5
        return result

    monkeypatch.setattr(kube, "call", late)
    with pytest.raises(SyncRecoveryUnavailable, match="timed out"):
        verify_engine(kube, IDENT, EXPECTED, threading.Event())
    assert len(kube.calls) == expire_after


def test_expected_identity_is_captured_before_reads_and_not_mutated(monkeypatch):
    kube = ReadOnlyKube()
    expected = deepcopy(EXPECTED)
    original = kube.call

    def changed(*args, **kwargs):
        expected["pod_uid"] = "untrusted-new-value"
        return original(*args, **kwargs)

    monkeypatch.setattr(kube, "call", changed)
    assert verify_engine(kube, IDENT, expected, threading.Event()) == EXPECTED


def test_real_blocked_ownership_process_is_cancelled_and_reaped_before_second_read(monkeypatch):
    kube, cancelled, started = ReadOnlyKube(), threading.Event(), threading.Event()
    children, errors = [], []
    original_popen = subprocess.Popen

    def popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        started.set()
        return child

    def blocked(*args, **kwargs):
        kube.calls.append((args, kwargs))
        return run([sys.executable, "-c", "import time;time.sleep(60)"], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(kube, "call", blocked)

    def verify():
        try:
            verify_engine(kube, IDENT, EXPECTED, cancelled)
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=verify)
    worker.start()
    try:
        assert started.wait(3)
        before = time.monotonic()
        cancelled.set()
        worker.join(timeout=3)
        assert not worker.is_alive() and time.monotonic() - before < 3
        assert len(errors) == 1 and isinstance(errors[0], SyncRecoveryUnavailable)
        assert len(kube.calls) == len(children) == 1
        assert children[0].poll() is not None
        assert not any(thread.name == "podgrove-command-cancel" for thread in threading.enumerate())
    finally:
        cancelled.set()
        worker.join(timeout=3)
