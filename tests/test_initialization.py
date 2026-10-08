"""Storage deadlines track observed work rather than application build duration."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from podgrove import initialization
from podgrove.errors import PodgroveError
from podgrove.initialization import StorageInitTimeout, StorageInitWatch


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(initialization.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(initialization.time, "time", lambda: now[0] + 1000)
    return now


def pod(start=1100):
    return {"metadata": {"name": "pg-fixture-0", "uid": "original"},
            "spec": {"nodeName": "node-fixture", "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": "pg-fixture"}}]},
            "status": {"initContainerStatuses": [{"name": "storage", "state": {"running": {
                "startedAt": datetime.fromtimestamp(start, timezone.utc).isoformat()}}}]}}


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "5"])
def test_invalid_init_budget_is_refused(timeout):
    with pytest.raises(PodgroveError, match="finite and positive"):
        StorageInitWatch("fixture", timeout)


def test_running_initializer_expires_with_node_and_pvc(clock):
    watch = StorageInitWatch("fixture", 5)
    watch.observe(pod())
    assert watch.limit(900) == 105
    clock[0] = 105
    with pytest.raises(StorageInitTimeout) as error:
        watch.check()
    assert "Pod fixture/pg-fixture-0; node node-fixture; PVC fixture/pg-fixture" in str(error.value)
    assert "after 5s" in str(error.value) and "cause is not established" in str(error.value)


def test_existing_hung_initializer_does_not_get_a_fresh_budget(clock):
    with pytest.raises(StorageInitTimeout):
        StorageInitWatch("fixture", 5).observe(pod(start=1090))


def test_restart_and_missing_status_do_not_extend_started_work(clock):
    watch, value = StorageInitWatch("fixture", 5), pod()
    watch.observe(value)
    clock[0] = 104
    watch.observe(pod(start=1104))
    assert watch.limit(900) == 105
    value["status"] = {}
    watch.observe(value)
    clock[0] = 105
    with pytest.raises(StorageInitTimeout):
        watch.observe(value)


def test_completed_init_does_not_limit_build_or_service_readiness(clock):
    watch, value = StorageInitWatch("fixture", 5), pod()
    watch.observe(value)
    clock[0] = 104
    value["status"]["initContainerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}
    watch.observe(value)
    clock[0] = 1000
    assert watch.limit(5000) == 5000
    value["status"] = {}
    watch.observe(value)
    watch.check()


def test_missing_observation_does_not_reset_same_pod_initializer(clock):
    watch, value = StorageInitWatch("fixture", 5), pod()
    watch.observe(value)
    watch.observe(None)
    value["status"] = {}
    clock[0] = 105
    with pytest.raises(StorageInitTimeout, match="node node-fixture"):
        watch.observe(value)


def test_image_pull_and_scheduling_keep_the_outer_startup_budget(clock):
    watch, value = StorageInitWatch("fixture", 5), pod()
    value["status"]["initContainerStatuses"][0]["state"] = {"waiting": {"reason": "PodInitializing"}}
    watch.observe(value)
    clock[0] = 1000
    assert watch.limit(5000) == 5000


def test_crash_loop_keeps_original_initializer_deadline(clock):
    watch, value = StorageInitWatch("fixture", 5), pod()
    watch.observe(value)
    status = value["status"]["initContainerStatuses"][0]
    status["lastState"] = {"terminated": {"exitCode": 1, "startedAt": "1970-01-01T00:18:20Z"}}
    status["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
    clock[0] = 105
    with pytest.raises(StorageInitTimeout):
        watch.observe(value)


def test_verified_new_pod_gets_its_own_init_budget(clock):
    watch, original = StorageInitWatch("fixture", 5), pod()
    watch.observe(original)
    clock[0] = 104
    watch.observe(None)
    replacement = deepcopy(pod(start=1104))
    replacement["metadata"]["uid"] = "replacement"
    watch.observe(replacement)
    assert watch.limit(900) == 109


@pytest.mark.parametrize("stamp", [None, "invalid", "9999-99-99T99:99:99Z", "1970-01-01T00:18:20", "2099-01-01T00:00:00Z"])
def test_bad_or_future_timestamp_uses_bounded_local_observation(clock, stamp):
    watch, value = StorageInitWatch("fixture", 5), pod()
    value["status"]["initContainerStatuses"][0]["state"]["running"]["startedAt"] = stamp
    watch.observe(value)
    assert watch.limit(900) == 105
    clock[0] = 105
    with pytest.raises(StorageInitTimeout):
        watch.check()
