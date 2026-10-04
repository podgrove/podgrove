"""Bounded graceful cleanup preserves ownership, storage, and retry evidence."""
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from podgrove import kube as module
from podgrove.errors import PodgroveError
from podgrove.kube import CleanupPending, ENVIRONMENT, MANAGED, Kube

IDENT = "123456abcdef"
KINDS = {"statefulsets": "StatefulSet", "pods": "Pod", "persistentvolumeclaims": "PersistentVolumeClaim",
         "configmaps": "ConfigMap", "networkpolicies": "NetworkPolicy", "services": "Service",
         "poddisruptionbudgets": "PodDisruptionBudget"}


def resource(kind, name=None):
    return {"apiVersion": "v1", "kind": kind, "metadata": {
        "name": name or f"pg-{IDENT}" + ("-0" if kind == "Pod" else ""), "namespace": "team",
        "uid": "uid-" + kind, "resourceVersion": "1", "labels": {MANAGED: "podgrove", ENVIRONMENT: IDENT}},
        "data": {"namespace_mode": "shared"}}


class Cluster:
    def __init__(self):
        self.objects = {(kind, resource(kind)["metadata"]["name"]): resource(kind) for kind in KINDS.values()}
        self.now = 0
        self.calls, self.deletes = [], []
        self.stuck = set()
        self.hook = None

    def sleep(self, seconds):
        self.now += seconds

    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        assert kwargs["timeout"] > 0
        assert args[-1].startswith("--request-timeout=") and args[-1] != "--request-timeout=0"
        assert "--force" not in args and "--grace-period=0" not in args
        if self.hook:
            self.hook(args, kwargs)
        if args[0] == "get":
            if "-l" in args:
                assert set(args[1].split(",")) == set(KINDS)
                assert args[args.index("-l") + 1] == f"{MANAGED}=podgrove,{ENVIRONMENT}={IDENT}"
                assert "--ignore-not-found" not in args
                body = {"items": [value for value in self.objects.values()
                                  if value["metadata"]["labels"] == {MANAGED: "podgrove", ENVIRONMENT: IDENT}]}
            else:
                body = self.objects.get((KINDS[args[1]], args[2]))
            return SimpleNamespace(stdout=json.dumps(body) if body else "", returncode=0)
        assert args[0:2] == ("delete", "--raw") and args[3:5] == ("-f", "-")
        path = args[2].split("/")
        assert path[-3] == "team"
        key = KINDS[path[-2]], path[-1]
        body = self.objects[key]
        options = json.loads(kwargs["input"])
        assert options["preconditions"] == {field: body["metadata"][field] for field in ("uid", "resourceVersion")}
        assert "gracePeriodSeconds" not in options
        assert "finalizers" not in options
        if key[0] in ("PersistentVolumeClaim", "ConfigMap"):
            assert not any(kind in ("StatefulSet", "Pod") for kind, _ in self.objects)
        if key[0] == "StatefulSet":
            assert options["propagationPolicy"] == "Foreground"
        self.deletes.append(key)
        if key[0] in self.stuck:
            body["metadata"].update(deletionTimestamp="2026-10-04T00:00:00Z", finalizers=["test/protection"])
        else:
            del self.objects[key]
            if key[0] == "StatefulSet" and "Pod" not in self.stuck:
                self.objects.pop(("Pod", f"pg-{IDENT}-0"), None)
        return SimpleNamespace(stdout=json.dumps({"kind": "Status", "status": "Success"}), returncode=0)


@pytest.fixture
def cluster(monkeypatch):
    cluster = Cluster()
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: cluster.now,
                                                       time=lambda: 1000 + cluster.now, sleep=cluster.sleep))
    kube = Kube("test-context", "team")
    kube.call = cluster.call
    return cluster, kube


def test_cleanup_normal_and_repeated_use_exact_preconditions_and_retain_namespace(cluster):
    world, kube = cluster
    kube.destroy(IDENT, timeout=8)
    assert not world.objects
    assert [kind for kind, _ in world.deletes] == ["StatefulSet", "PersistentVolumeClaim", "NetworkPolicy",
                                                 "Service", "PodDisruptionBudget", "ConfigMap"]
    count = len(world.deletes)
    kube.destroy(IDENT, timeout=8)
    assert len(world.deletes) == count
    assert all("namespaces/team/" in args[2] for args, _ in world.calls if args[0] == "delete")


@pytest.mark.parametrize("stuck", ["StatefulSet", "Pod"])
def test_stuck_terminating_engine_is_bounded_and_preserves_pvc_and_lease(cluster, stuck):
    world, kube = cluster
    world.stuck.add(stuck)
    with pytest.raises(CleanupPending) as captured:
        kube.destroy(IDENT, timeout=4)
    report = captured.value.report
    assert world.now <= 4
    assert report["inventory_complete"] is True
    assert report["observed_at"] is not None
    assert all(kind not in ("PersistentVolumeClaim", "ConfigMap") for kind, _ in world.deletes)
    pending = next(item for item in report["remaining"] if item["kind"] == stuck)
    assert pending == {"kind": stuck, "name": resource(stuck)["metadata"]["name"], "uid": "uid-" + stuck,
                       "deleting": True, "finalizers": ["test/protection"]}
    world.objects = {key: value for key, value in world.objects.items() if key[0] not in ("StatefulSet", "Pod")}
    kube.destroy(IDENT, timeout=4)
    assert not world.objects


def test_stuck_pvc_retains_lease_and_reports_finalizer_without_force(cluster):
    world, kube = cluster
    world.stuck.add("PersistentVolumeClaim")
    with pytest.raises(CleanupPending) as captured:
        kube.destroy(IDENT, timeout=4)
    assert {item["kind"] for item in captured.value.report["remaining"]} == {"ConfigMap", "PersistentVolumeClaim"}
    assert ("ConfigMap", f"pg-{IDENT}") not in world.deletes


@pytest.mark.parametrize("field,value", [("uid", "replacement"), ("labels", {MANAGED: "other", ENVIRONMENT: IDENT})])
def test_changed_engine_identity_or_labels_stops_cleanup_and_storage_deletion(cluster, field, value):
    world, kube = cluster
    world.stuck.add("StatefulSet")
    def hook(args, kwargs):
        if world.deletes and args[0] == "get":
            world.objects["StatefulSet", f"pg-{IDENT}"]["metadata"][field] = value
    world.hook = hook
    with pytest.raises(CleanupPending) as captured:
        kube.destroy(IDENT, timeout=4)
    assert not captured.value.report["inventory_complete"]
    assert world.deletes == [("StatefulSet", f"pg-{IDENT}")]
    assert "refusing" in captured.value.reason


def test_initial_foreign_lease_refuses_every_mutation(cluster):
    world, kube = cluster
    world.objects["ConfigMap", f"pg-{IDENT}"]["metadata"]["labels"][MANAGED] = "other"
    with pytest.raises(CleanupPending, match="foreign"):
        kube.destroy(IDENT)
    assert not world.deletes


def test_uncertain_delete_is_not_replayed_and_first_error_survives_failed_readback(cluster):
    world, kube = cluster
    def hook(args, kwargs):
        if args[0] == "delete":
            raise PodgroveError("delete result unavailable")
        if any(call[0][0] == "delete" for call in world.calls):
            raise PodgroveError("inventory unavailable")
    world.hook = hook
    with pytest.raises(CleanupPending) as captured:
        kube.destroy(IDENT, timeout=4)
    assert captured.value.reason == "delete result unavailable"
    assert captured.value.report["inventory_complete"] is False
    assert len(captured.value.report["remaining"]) == 7
    assert len([args for args, _ in world.calls if args[0] == "delete"]) == 1


@pytest.mark.parametrize("payload", ["", "{}", '{"items":null}', '{"items":[],"metadata":{"continue":"next"}}'])
def test_malformed_or_partial_inventory_never_deletes(cluster, payload):
    world, kube = cluster
    original = kube.call
    def call(*args, **kwargs):
        if "-l" in args:
            return SimpleNamespace(stdout=payload)
        return original(*args, **kwargs)
    kube.call = call
    with pytest.raises(CleanupPending):
        kube.destroy(IDENT, timeout=4)
    assert not world.deletes


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "4"])
def test_invalid_budget_refused_before_any_cluster_call(cluster, timeout):
    world, kube = cluster
    with pytest.raises(PodgroveError, match="positive finite"):
        kube.destroy(IDENT, timeout=timeout)
    assert world.calls == []


def test_request_budgets_shrink_with_one_absolute_deadline(cluster):
    world, kube = cluster
    world.stuck.add("StatefulSet")
    def hook(args, kwargs):
        world.now += min(.2, kwargs["timeout"])
    world.hook = hook
    with pytest.raises(CleanupPending):
        kube.destroy(IDENT, timeout=2)
    assert world.now <= 2.001
    assert world.calls[-1][1]["timeout"] < world.calls[0][1]["timeout"]


def test_actual_blocked_kubectl_child_is_reaped_within_cleanup_budget(tmp_path, monkeypatch):
    marker = tmp_path / "pid"
    script = tmp_path / "blocked.py"
    script.write_text("import os,pathlib,time\npathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid()))\ntime.sleep(30)\n")
    kube = Kube("test-context", "team")
    monkeypatch.setattr(kube, "command", lambda *args: [sys.executable, "-I", "-B", str(script)])
    started = time.monotonic()
    with pytest.raises(CleanupPending) as captured:
        kube.destroy(IDENT, timeout=.5)
    assert time.monotonic() - started < 2
    assert captured.value.report["inventory_complete"] is False
    pid = int(marker.read_text())
    assert subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True).returncode == 1


@pytest.mark.parametrize("change", ["namespace", "mode", "uid", "rv", "name", "finalizer"])
def test_bad_or_foreign_resource_metadata_refuses_all_deletes(cluster, change):
    world, kube = cluster
    item = world.objects["ConfigMap", f"pg-{IDENT}"]
    if change == "mode":
        item["data"]["namespace_mode"] = "exclusive"
    elif change == "rv":
        item["metadata"]["resourceVersion"] = ""
    elif change == "finalizer":
        item["metadata"]["finalizers"] = ["unsafe\noutput"]
    else:
        item["metadata"][change] = "foreign" if change == "namespace" else ""
    with pytest.raises(CleanupPending):
        kube.destroy(IDENT, timeout=4)
    assert not world.deletes


def test_delete_resource_version_conflict_does_not_retry_or_delete_storage(cluster):
    world, kube = cluster
    def hook(args, kwargs):
        if args[0] == "delete":
            raise PodgroveError("Conflict: resourceVersion precondition failed")
    world.hook = hook
    with pytest.raises(CleanupPending, match="resourceVersion precondition") as captured:
        kube.destroy(IDENT, timeout=4)
    assert captured.value.report["inventory_complete"] is True
    assert len([args for args, _ in world.calls if args[0] == "delete"]) == 1
    assert not world.deletes


def test_inventory_malformed_metadata_is_reported_as_incomplete(cluster):
    world, kube = cluster
    original = kube.call
    def call(*args, **kwargs):
        if "-l" in args:
            return SimpleNamespace(stdout=json.dumps({"items": [], "metadata": []}))
        return original(*args, **kwargs)
    kube.call = call
    with pytest.raises(CleanupPending) as captured:
        kube.destroy(IDENT, timeout=4)
    assert captured.value.report["inventory_complete"] is False
    assert not world.deletes


@pytest.mark.parametrize("stage", ["response", "parse", "validation"])
def test_late_cleanup_observation_cannot_report_success_or_complete_inventory(cluster, monkeypatch, stage):
    world, kube = cluster
    if stage == "response":
        world.objects.clear()
        world.hook = lambda args, kwargs: setattr(world, "now", 5) if "-l" in args else None
    elif stage == "parse":
        world.objects.clear()
        def loads(raw):
            value = json.loads(raw)
            if "items" in value:
                world.now = 5
            return value
        monkeypatch.setattr(module, "json", SimpleNamespace(loads=loads, dumps=json.dumps))
    else:
        world.objects.clear()
        class LateMetadata(dict):
            def get(self, key, default=None):
                world.now = 5
                return super().get(key, default)
        def loads(raw):
            value = json.loads(raw)
            if "items" in value:
                value["metadata"] = LateMetadata()
            return value
        monkeypatch.setattr(module, "json", SimpleNamespace(loads=loads, dumps=json.dumps))
    with pytest.raises(CleanupPending, match="deadline") as captured:
        kube.destroy(IDENT, timeout=1)
    assert captured.value.report["inventory_complete"] is False
    assert not world.deletes


def test_late_final_readback_keeps_first_error_and_incomplete_inventory(cluster):
    world, kube = cluster
    failed = []
    def hook(args, kwargs):
        if args[0] == "delete":
            failed.append(True)
            raise PodgroveError("original uncertain delete")
        if failed:
            world.now = 5
    world.hook = hook
    with pytest.raises(CleanupPending) as captured:
        kube.destroy(IDENT, timeout=1)
    assert captured.value.reason == "original uncertain delete"
    assert captured.value.report["inventory_complete"] is False


def test_pinned_named_read_cannot_return_different_resource(cluster):
    world, kube = cluster
    original = kube.call
    def call(*args, **kwargs):
        if args[0] == "get" and args[1:3] == ("statefulsets", f"pg-{IDENT}"):
            return SimpleNamespace(stdout=json.dumps(resource("Service")))
        return original(*args, **kwargs)
    kube.call = call
    with pytest.raises(CleanupPending, match="different resource"):
        kube.destroy(IDENT, timeout=4)
    assert world.deletes == [("StatefulSet", f"pg-{IDENT}")]
