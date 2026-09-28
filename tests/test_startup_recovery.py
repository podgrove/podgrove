"""Startup replay requires stable controller/storage ownership and a ready Pod."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from podgrove.errors import PodgroveError
from podgrove.kube import MANAGED, ENVIRONMENT
from podgrove import startup_recovery as recovery

IDENT = "012345abcdef"


class Clock:
    now = 100.0

    def sleep(self, seconds):
        self.now += seconds


class Kube:
    context = "test-context"
    namespace = "test-namespace"

    def __init__(self):
        labels = {MANAGED: "podgrove", ENVIRONMENT: IDENT}
        def metadata(kind):
            return {"name": "pg-" + IDENT + ("-0" if kind == "pod" else ""),
                    "namespace": self.namespace, "labels": dict(labels), "uid": kind + "-original"}
        self.objects = {kind: {"metadata": metadata(kind)} for kind in ("statefulset", "pvc", "pod")}
        self.objects["statefulset"]["spec"] = {"replicas": 1, "template": {"spec": {"containers": []}}}
        pod = self.objects["pod"]
        pod["metadata"]["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": "StatefulSet",
                                                "name": "pg-" + IDENT, "uid": "statefulset-original", "controller": True}]
        pod["spec"] = {"volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "pg-" + IDENT}}]}
        pod["status"] = {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        value = self.objects[args[1]]
        return SimpleNamespace(returncode=0, stdout=json.dumps(value) if value is not None else "")


@pytest.fixture
def bound(monkeypatch):
    clock = Clock()
    class Event:
        cancelled = False

        def is_set(self):
            return self.cancelled

        def set(self):
            self.cancelled = True

        def wait(self, seconds):
            clock.sleep(seconds)
            return self.cancelled

    monkeypatch.setattr(recovery.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(recovery.threading, "Event", Event)
    kube = Kube()
    anchor = recovery.capture_anchor(kube, IDENT)
    return kube, anchor, clock


def test_owned_replacement_requires_two_ready_reads_with_original_controller_and_pvc(bound):
    kube, anchor, clock = bound
    kube.objects["pod"]["metadata"]["uid"] = "replacement"
    proof = recovery.StartupRecovery(kube, IDENT, anchor, 3)
    assert proof.wait() == "replacement"
    assert clock.now == 100.25
    assert [args[1] for args, _ in kube.calls] == ["statefulset", "pvc"] + ["statefulset", "pvc", "pod"] * 2
    assert all(args[0] == "get" and kwargs["timeout"] <= 15 for args, kwargs in kube.calls)
    assert anchor["pvc_uid"] == "pvc-original"


@pytest.mark.parametrize("kind,field,value", [
    ("statefulset", "uid", "replaced"), ("pvc", "uid", "replaced"),
    ("statefulset", "deletionTimestamp", "now"), ("pvc", "deletionTimestamp", "now"),
    ("pod", "namespace", "foreign"), ("pod", "name", "another-pod"),
])
def test_changed_persistent_identity_or_foreign_pod_is_refused(bound, kind, field, value):
    kube, anchor, _ = bound
    kube.objects[kind]["metadata"][field] = value
    with pytest.raises(PodgroveError):
        recovery.StartupRecovery(kube, IDENT, anchor, 3).wait()


@pytest.mark.parametrize("change", ["settings", "pod-owner", "pod-volume", "foreign-label", "missing-pvc"])
def test_no_replay_if_resource_binding_changed(bound, change):
    kube, anchor, _ = bound
    if change == "settings":
        kube.objects["statefulset"]["spec"]["replicas"] = 2
    elif change == "pod-owner":
        kube.objects["pod"]["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    elif change == "pod-volume":
        kube.objects["pod"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "foreign"
    elif change == "foreign-label":
        kube.objects["pod"]["metadata"]["labels"][ENVIRONMENT] = "ffffffffffff"
    else:
        kube.objects["pvc"] = None
    with pytest.raises(PodgroveError):
        recovery.StartupRecovery(kube, IDENT, anchor, 3).wait()


def test_missing_and_terminating_pods_wait_for_the_owned_ready_replacement(bound, monkeypatch):
    kube, anchor, _ = bound
    ready = deepcopy(kube.objects["pod"])
    ready["metadata"]["uid"] = "replacement"
    terminating = deepcopy(ready)
    terminating["metadata"]["deletionTimestamp"] = "now"
    pending = deepcopy(ready)
    pending["status"]["conditions"][0]["status"] = "False"
    versions = iter([None, terminating, pending, ready, ready])
    call = kube.call
    def changing(*args, **kwargs):
        if args[1] == "pod":
            kube.objects["pod"] = next(versions)
        return call(*args, **kwargs)
    monkeypatch.setattr(kube, "call", changing)
    assert recovery.StartupRecovery(kube, IDENT, anchor, 3).wait() == "replacement"


def test_combined_deadline_clips_each_read_and_rejects_late_ready(bound, monkeypatch):
    kube, anchor, clock = bound
    call = kube.call
    def slow(*args, **kwargs):
        clock.now += 0.4
        return call(*args, **kwargs)
    monkeypatch.setattr(kube, "call", slow)
    with pytest.raises(PodgroveError, match="deadline"):
        recovery.StartupRecovery(kube, IDENT, anchor, 1).wait()
    assert len(kube.calls) == 5
    assert kube.calls[-1][1]["timeout"] == pytest.approx(0.2)


def test_pending_pod_has_one_absolute_deadline(bound):
    kube, anchor, clock = bound
    kube.objects["pod"]["status"]["phase"] = "Pending"
    with pytest.raises(PodgroveError, match="deadline"):
        recovery.StartupRecovery(kube, IDENT, anchor, 1).wait()
    assert clock.now == 101


@pytest.mark.parametrize("field,value", [("context", "other"), ("namespace", "other"),
                                          ("identity", "other"), ("pvc_uid", None)])
def test_saved_anchor_cannot_redirect_recovery(bound, field, value):
    kube, anchor, _ = bound
    anchor[field] = value
    with pytest.raises(PodgroveError, match="anchor"):
        recovery.StartupRecovery(kube, IDENT, anchor, 1)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_deadline_refused_before_reads(bound, timeout):
    kube, anchor, _ = bound
    before = len(kube.calls)
    with pytest.raises(PodgroveError, match="finite and positive"):
        recovery.StartupRecovery(kube, IDENT, anchor, timeout)
    with pytest.raises(PodgroveError, match="finite and positive"):
        recovery.capture_anchor(kube, IDENT, timeout)
    assert len(kube.calls) == before


def test_cancellation_prevents_reads_and_is_passed_to_each_process(bound):
    kube, anchor, _ = bound
    guard = recovery.StartupRecovery(kube, IDENT, anchor, 1)
    assert guard.wait() == "pod-original"
    assert all(kwargs["cancel_event"] is guard.cancel for _, kwargs in kube.calls[2:])
    before = len(kube.calls)
    guard.cancel.set()
    with pytest.raises(PodgroveError, match="cancelled"):
        guard.wait()
    assert len(kube.calls) == before
