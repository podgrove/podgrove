import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from podgrove import kube as kube_module
from podgrove.errors import PodgroveError
from podgrove.bootstrap import PROVISIONING_MARKER, provisioning_marker
from podgrove.kube import DEDICATED, ENVIRONMENT, MANAGED, Kube, engine_pod_manifest, engine_pod_name, manifests, shared_namespace, validate_tainted_nodes

IDENT = "123456abcdef"


def pod_from(resources):
    return engine_pod_manifest(next(item for item in resources if item["kind"] == "StatefulSet"))


def existing_controller(resources=None):
    resources = resources or manifests("default", IDENT, Path("/worktree/test"), "small", 600)
    controller = copy.deepcopy(next(item for item in resources if item["kind"] == "StatefulSet"))
    controller["metadata"].update(uid="controller-uid", resourceVersion="42")
    return controller


def readiness_reads(pods, controller=None):
    controller = controller or existing_controller()
    sequence = iter(pods) if isinstance(pods, list) else None
    def get(kind, name):
        if kind == "statefulset":
            assert name == f"pg-{IDENT}"
            return controller
        assert kind == "pod" and name == engine_pod_name(IDENT)
        return next(sequence) if sequence is not None else pods
    return Mock(side_effect=get)


def with_delete_storage(kube):
    """Supply an administrator-selected class to unrelated workload tests."""
    original = kube.check_storage
    def check(resources):
        for resource in resources:
            if resource["kind"] == "PersistentVolumeClaim":
                resource["spec"].setdefault("storageClassName", "gp3")
        return original(resources)
    kube.check_storage = Mock(side_effect=check)


def node():
    return {"metadata": {"labels": {DEDICATED: "true", "kubernetes.io/os": "linux"}},
            "spec": {"taints": [{"key": "dedicated", "value": "podgrove", "effect": "NoSchedule"}]},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


def test_explicit_context_and_namespace_required():
    with pytest.raises(PodgroveError, match="context"):
        Kube("", "podgrove-testing")
    with pytest.raises(PodgroveError, match="namespace"):
        Kube("fixture:development", "Production")
    assert Kube("custom-cluster", "production").namespace == "production"
    command = Kube("fixture:development", "podgrove-testing").command("get", "pods")
    assert command[:5] == ["kubectl", "--context", "fixture:development", "--namespace", "podgrove-testing"]


def test_default_bootstrap_marker_is_reused_without_adoption_or_metadata_changes():
    marker = provisioning_marker("default", "shared")
    before = copy.deepcopy(marker)
    kube = Kube("test-context", "default")
    kube.get = Mock(return_value=marker)
    kube.call = Mock()
    assert kube.ensure_namespace(IDENT, {MANAGED: "podgrove", ENVIRONMENT: IDENT}) is False
    kube.get.assert_called_once_with("configmap", PROVISIONING_MARKER)
    kube.call.assert_not_called()
    assert marker == before


def test_missing_default_bootstrap_is_never_created():
    kube = Kube("test-context", "default")
    kube.get = Mock(return_value={})
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="Bootstrap ConfigMap default/podgrove-bootstrap is missing"):
        kube.ensure_namespace(IDENT)
    kube.call.assert_not_called()


def waiting_pod(*, phase="Pending", ready=False):
    pod = engine_pod_manifest(existing_controller())
    pod["metadata"]["uid"] = "original-pod-uid"
    pod["status"] = {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}
    return pod


@pytest.fixture
def readiness_clock(monkeypatch):
    clock = {"now": 0, "sleeps": []}
    monkeypatch.setattr(kube_module.time, "monotonic", lambda: clock["now"])
    def sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds
    monkeypatch.setattr(kube_module.time, "sleep", sleep)
    return clock


def test_readiness_detects_deleted_pod_after_one_poll_instead_of_waiting_full_timeout(readiness_clock):
    kube = Kube("test-context", "default")
    kube.get = readiness_reads([waiting_pod(), {}])
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="was deleted or no longer exists"):
        kube.wait(IDENT, 900)
    assert readiness_clock["now"] == 2
    assert kube.get.call_count == 4
    kube.call.assert_not_called()


@pytest.mark.parametrize("failure", ["deleting", "foreign-manager", "foreign-identity", "Failed", "Succeeded"])
def test_readiness_refuses_missing_deleting_foreign_and_terminal_pods_even_if_ready(failure, readiness_clock):
    pod = waiting_pod(phase="Running", ready=True)
    if failure == "missing":
        pod = {}
    elif failure == "deleting":
        pod["metadata"]["deletionTimestamp"] = "2026-09-25T10:35:00Z"
    elif failure == "foreign-manager":
        pod["metadata"]["labels"][MANAGED] = "someone-else"
    elif failure == "foreign-identity":
        pod["metadata"]["labels"][ENVIRONMENT] = "another-worktree"
    else:
        pod["status"].update(phase=failure, reason="Evicted", message="The node was low on resource: ephemeral-storage")
    kube = Kube("test-context", "default")
    kube.get = readiness_reads(pod)
    kube.call = Mock()
    with pytest.raises(PodgroveError) as error:
        kube.wait(IDENT, 900)
    assert f"pg-{IDENT}-0" in str(error.value)
    if failure in ("Failed", "Succeeded"):
        assert "Evicted" in str(error.value) and "ephemeral-storage" in str(error.value)
    if failure == "deleting":
        assert "2026-09-25T10:35:00Z" in str(error.value)
    assert readiness_clock["sleeps"] == []
    kube.call.assert_not_called()


def test_readiness_does_not_attach_to_replacement_pod_with_same_name(readiness_clock):
    replacement = waiting_pod(phase="Running", ready=True)
    replacement["metadata"]["uid"] = "replacement-pod-uid"
    kube = Kube("test-context", "default")
    kube.get = readiness_reads([waiting_pod(), replacement])
    with pytest.raises(PodgroveError, match="was replaced"):
        kube.wait(IDENT, 900)
    assert readiness_clock["now"] == 2


def test_readiness_timeout_reports_scheduling_and_init_failure_with_bounded_poll_interval(readiness_clock):
    pod = waiting_pod()
    pod["status"]["conditions"].append({"type": "PodScheduled", "status": "False", "message": "Insufficient memory"})
    pod["status"]["initContainerStatuses"] = [{"name": "storage", "state": {"waiting": {
        "reason": "ImagePullBackOff", "message": "Registry unavailable"}}}]
    kube = Kube("test-context", "default")
    kube.get = readiness_reads(pod)
    with pytest.raises(PodgroveError, match="Timed out after 5s") as error:
        kube.wait(IDENT, 5)
    assert "Insufficient memory" in str(error.value)
    assert "storage: ImagePullBackOff Registry unavailable" in str(error.value)
    assert readiness_clock["sleeps"] == [2, 2, 1]
    assert readiness_clock["now"] == 5


def test_readiness_accepts_same_owned_pod_once_ready(readiness_clock):
    kube = Kube("test-context", "default")
    kube.get = readiness_reads([waiting_pod(), waiting_pod(phase="Running", ready=True)])
    kube.call = Mock()
    kube.wait(IDENT, 900)
    assert readiness_clock["now"] == 2
    kube.call.assert_not_called()


def test_readiness_waits_for_initial_controller_creation_without_creating_pod(readiness_clock):
    kube = Kube("test-context", "default")
    kube.get = readiness_reads([{}, {}, waiting_pod(phase="Running", ready=True)])
    kube.call = Mock()
    kube.wait(IDENT, 900)
    assert readiness_clock["now"] == 4
    kube.call.assert_not_called()


def test_readiness_missing_pod_deadline_reports_controller_failure(readiness_clock):
    controller = existing_controller()
    controller["status"] = {"conditions": [{"type": "ReplicaFailure", "message": "Admission denied required privileged engine"}]}
    kube = Kube("test-context", "default")
    kube.get = readiness_reads({}, controller)
    with pytest.raises(PodgroveError, match="Admission denied required privileged engine"):
        kube.wait(IDENT, 5)
    assert readiness_clock["now"] == 5


@pytest.mark.parametrize("mismatch", ["missing", "uid", "kind", "name", "controller", "apiVersion"])
def test_readiness_requires_genuine_statefulset_owner_reference(mismatch, readiness_clock):
    pod = waiting_pod(phase="Running", ready=True)
    if mismatch == "missing":
        pod["metadata"].pop("ownerReferences")
    else:
        pod["metadata"]["ownerReferences"][0][mismatch] = False if mismatch == "controller" else "foreign"
    kube = Kube("test-context", "default")
    kube.get = readiness_reads(pod)
    with pytest.raises(PodgroveError, match="not controlled by the owned StatefulSet"):
        kube.wait(IDENT, 900)
    assert readiness_clock["now"] == 0


@pytest.mark.parametrize("labels", [{}, {MANAGED: "cluster-admin"}, {MANAGED: "podgrove"}])
def test_default_cleanup_never_deletes_namespace_and_requires_both_resource_owner_labels(labels):
    kube = Kube("test-context", "default")
    kube.get = Mock(side_effect=lambda kind, name: {"metadata": {"name": "default", "labels": labels}}
                    if kind == "namespace" else {})
    kube.call = Mock()
    kube.destroy(IDENT)
    assert kube.call.call_args_list == [
        call("delete", "statefulset", "-l", f"{MANAGED}=podgrove,{ENVIRONMENT}={IDENT}",
             "--ignore-not-found", "--cascade=foreground", "--wait=true", "--timeout=120s", "--request-timeout=0", timeout=130),
        call("delete", "pod,pvc,configmap,networkpolicy,service", "-l", f"{MANAGED}=podgrove,{ENVIRONMENT}={IDENT}",
             "--ignore-not-found", "--wait=true", "--timeout=120s", timeout=130),
    ]


@pytest.mark.parametrize("kind", ["NetworkPolicy", "PersistentVolumeClaim", "ConfigMap", "Service", "StatefulSet"])
@pytest.mark.parametrize("labels", [{}, {MANAGED: "cluster-admin", ENVIRONMENT: IDENT},
                                    {MANAGED: "podgrove", ENVIRONMENT: "another-worktree"}])
def test_default_refuses_foreign_resource_even_though_namespace_is_shared(kind, labels):
    resource = next(item for item in manifests("default", IDENT, Path("/worktree/test"), "small", 600)
                    if item["kind"] == kind)
    kube = Kube("test-context", "default")
    kube.get = Mock(return_value={"metadata": {"labels": labels}})
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="Refusing to modify unowned"):
        kube.create_environment([resource], IDENT)
    kube.call.assert_not_called()


def test_default_manifest_is_namespaced_owned_and_policy_does_not_select_foreign_pods():
    resources = manifests("default", IDENT, Path("/worktree/test"), "small", 600)
    for resource in resources:
        assert resource["kind"] != "Namespace"
        assert resource["metadata"]["namespace"] == "default"
        assert resource["metadata"]["labels"][MANAGED] == "podgrove"
        assert resource["metadata"]["labels"][ENVIRONMENT] == IDENT
    lease = next(item for item in resources if item["kind"] == "ConfigMap")
    assert lease["data"]["namespace_mode"] == "shared"
    policy = next(item for item in resources if item["kind"] == "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"] == {MANAGED: "podgrove", ENVIRONMENT: IDENT}
    assert shared_namespace("default") and shared_namespace("podgrove-testing")
    assert not shared_namespace("wt-test")


def test_legacy_exclusive_cleanup_always_retains_namespace():
    kube = Kube("test-context", "wt-test")
    kube.get = Mock(return_value={})
    kube.call = Mock()
    kube.destroy(IDENT)
    assert [item.args[1] for item in kube.call.call_args_list] == [
        "statefulset", "pod,pvc,configmap,networkpolicy,service"]
    kube.get.assert_called_once_with("configmap", f"pg-{IDENT}")


@pytest.mark.parametrize("mode", ["shared", "tainted"])
def test_preflight_never_inspects_cluster_node_inventory(mode):
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(side_effect=AssertionError("No resource inventory during preflight"))
    kube.call = Mock(return_value=SimpleNamespace(returncode=0, stdout="yes"))
    kube.preflight(node_mode=mode)
    kube.get.assert_not_called()
    assert all(item.args[:2] == ("auth", "can-i") for item in kube.call.call_args_list)


@pytest.mark.parametrize("resource,verb", [
    ("pods/portforward", "create"), ("pods/portforward", "get"),
    ("pods/exec", "create"), ("pods/exec", "get"),
])
def test_missing_stream_permission_fails_before_creation(resource, verb):
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(return_value={"items": [node()]})
    kube.call = Mock(side_effect=lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="no" if resource in args and verb in args else "yes"))
    with pytest.raises(PodgroveError, match=f"{verb} {resource}"):
        kube.preflight()
    assert all(call.args[0] == "auth" for call in kube.call.call_args_list)


@pytest.mark.parametrize("labels", [{}, {MANAGED: "another-manager"},
                                     {MANAGED: "podgrove", ENVIRONMENT: "different-owner"}])
def test_destroy_never_deletes_when_lease_is_foreign(labels):
    kube = Kube("test-context", "wt-test")
    kube.get = Mock(return_value={"metadata": {"name": f"pg-{IDENT}", "namespace": "wt-test", "labels": labels}})
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="foreign"):
        kube.destroy(IDENT)
    kube.call.assert_not_called()


def test_testing_namespace_cleanup_selects_only_owned_environment():
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(side_effect=lambda kind, name: {"metadata": {"name": "podgrove-testing", "labels": {
        MANAGED: "podgrove"}}} if kind == "namespace" else {})
    kube.call = Mock()
    kube.destroy(IDENT)
    args = kube.call.call_args.args
    assert args[0] == "delete"
    assert args[1] == "pod,pvc,configmap,networkpolicy,service"
    assert args[args.index("-l") + 1] == f"{MANAGED}=podgrove,{ENVIRONMENT}={IDENT}"
    assert "namespace" not in args
    assert "--all" not in args


def test_existing_resource_ownership_checked_before_modification():
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(return_value={"metadata": {"labels": {MANAGED: "someone-else", ENVIRONMENT: IDENT}}})
    kube.call = Mock()
    resources = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600)
    with pytest.raises(PodgroveError, match="Refusing to modify unowned"):
        kube.create_environment(resources, IDENT)
    kube.call.assert_not_called()


def test_absent_resource_is_created_without_adopting_a_concurrent_owner():
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(return_value={})
    kube.call = Mock(side_effect=PodgroveError("AlreadyExists"))
    resource = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600)[0]
    with pytest.raises(PodgroveError, match="AlreadyExists"):
        kube.create_environment([resource], IDENT)
    assert kube.call.call_args.args[0] == "create"


def test_existing_owned_policy_update_uses_resource_version():
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(return_value={"metadata": {"labels": {MANAGED: "podgrove", ENVIRONMENT: IDENT}, "resourceVersion": "42"}})
    kube.call = Mock()
    resource = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600)[0]
    kube.create_environment([resource], IDENT)
    assert kube.call.call_args.args[0] == "replace"
    assert json.loads(kube.call.call_args.kwargs["input"])["metadata"]["resourceVersion"] == "42"


@pytest.mark.parametrize("mode", ["shared", "tainted"])
def test_admission_rejection_leaves_no_new_pvc_policy_lease_or_pod(mode):
    resources = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600, node_mode=mode)
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(return_value={})
    calls = []
    def call(*args, **kwargs):
        calls.append((args, json.loads(kwargs["input"])))
        assert "--dry-run=server" in args, "admission must precede every real write"
        raise PodgroveError("ValidatingAdmissionPolicy fixture-deny-privileged denied the request")
    kube.call = call
    with pytest.raises(PodgroveError, match="admission preflight failed.*privileged Docker engine") as failure:
        kube.create_environment(resources, IDENT)
    assert "fixture-deny-privileged" in str(failure.value)
    assert "does not bypass admission policy" in str(failure.value)
    assert len(calls) == 1
    assert calls[0][0] == ("create", "--dry-run=server", "-f", "-")
    pod = calls[0][1]
    assert pod["kind"] == "Pod"
    assert pod["metadata"]["namespace"] == "podgrove-testing"
    assert pod["spec"]["containers"][0]["securityContext"]["privileged"] is True


def test_successful_admission_precedes_all_environment_creation():
    resources = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600)
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(return_value={})
    with_delete_storage(kube)
    kube.call = Mock(return_value=SimpleNamespace(returncode=0, stdout="pod/pg created (server dry run)"))
    kube.create_environment(resources, IDENT)
    calls = kube.call.call_args_list
    assert calls[0].args == ("create", "--dry-run=server", "-f", "-")
    assert json.loads(calls[0].kwargs["input"])["kind"] == "Pod"
    assert len(calls) == len(resources) + 1
    assert [json.loads(call.kwargs["input"])["kind"] for call in calls[1:]] == [
        "NetworkPolicy", "PersistentVolumeClaim", "ConfigMap", "Service", "StatefulSet",
    ]
    assert all("--dry-run=server" not in call.args for call in calls[1:])


def test_controller_has_single_replica_independent_pvc_and_no_fake_owner_reference():
    resources = manifests("default", IDENT, Path("/worktree/test"), "small", 600)
    controller = next(item for item in resources if item["kind"] == "StatefulSet")
    spec = controller["spec"]
    assert spec["replicas"] == 1
    assert spec["updateStrategy"] == {"type": "OnDelete"}
    assert spec["serviceName"] == f"pg-{IDENT}"
    assert "volumeClaimTemplates" not in spec
    assert spec["template"]["spec"]["volumes"] == [{"name": "data", "persistentVolumeClaim": {"claimName": f"pg-{IDENT}"}}]
    assert "ownerReferences" not in spec["template"]["metadata"]
    service = next(item for item in resources if item["kind"] == "Service")
    assert service["spec"] == {"clusterIP": "None", "selector": {MANAGED: "podgrove", ENVIRONMENT: IDENT}}
    pod = engine_pod_manifest(controller)
    assert pod["metadata"]["name"] == engine_pod_name(IDENT) == f"pg-{IDENT}-0"
    assert pod["spec"]["hostname"] == f"pg-{IDENT}-0"
    assert pod["spec"]["subdomain"] == f"pg-{IDENT}"
    assert "ownerReferences" not in pod["metadata"]
    assert not any(item["kind"] == "Pod" for item in resources)


@pytest.mark.parametrize("owned", [True, False])
def test_legacy_bare_pod_blocks_controller_before_any_creation(owned):
    resources = manifests("default", IDENT, Path("/worktree/test"), "small", 600)
    legacy = {"metadata": {"name": f"pg-{IDENT}", "labels": {MANAGED: "podgrove" if owned else "foreign"}}}
    kube = Kube("test-context", "default")
    kube.get = Mock(side_effect=lambda kind, name: legacy if kind == "pod" and name == f"pg-{IDENT}" else {})
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="legacy bare engine Pod.*second Docker engine.*down deletes environment data"):
        kube.create_environment(resources, IDENT)
    kube.call.assert_not_called()


@pytest.mark.parametrize("field", ["replicas", "image", "size", "storage", "strategy", "serviceName"])
def test_controller_drift_is_refused_without_updating_or_rolling_engine(field):
    resources = manifests("default", IDENT, Path("/worktree/test"), "small", 600)
    controller = existing_controller(resources)
    spec = controller["spec"]
    engine = spec["template"]["spec"]["containers"][0]
    if field == "replicas":
        spec["replicas"] = 2
    elif field == "image":
        engine["image"] = "docker:old-dind"
    elif field == "size":
        engine["resources"]["limits"]["memory"] = "1Gi"
    elif field == "storage":
        spec["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "foreign-volume"
    elif field == "strategy":
        spec["updateStrategy"]["type"] = "RollingUpdate"
    else:
        spec["serviceName"] = "foreign-service"
    kube = Kube("test-context", "default")
    kube.get = Mock(side_effect=lambda kind, name: controller if kind == "StatefulSet" else {})
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="different engine settings"):
        kube.create_environment(resources, IDENT)
    kube.call.assert_not_called()


def test_defaulted_controller_template_is_reused_without_replacement():
    resources = manifests("default", IDENT, Path("/worktree/test"), "small", 600)
    controller = existing_controller(resources)
    spec = controller["spec"]["template"]["spec"]
    spec.update(restartPolicy="Always", dnsPolicy="ClusterFirst", schedulerName="default-scheduler", securityContext={})
    spec.pop("tolerations")  # Empty slices and EnvVar values are omitted by API JSON.
    spec["containers"][0]["env"][0].pop("value")
    spec["containers"][0].update(imagePullPolicy="IfNotPresent", terminationMessagePolicy="File")
    controller["spec"].update(revisionHistoryLimit=10, podManagementPolicy="OrderedReady")
    pod = engine_pod_manifest(controller)
    kube = Kube("test-context", "default")
    kube.get = Mock(side_effect=lambda kind, name: controller if kind == "StatefulSet" else
                    pod if kind == "pod" and name == engine_pod_name(IDENT) else {})
    kube.call = Mock()
    with_delete_storage(kube)
    kube.create_environment(resources, IDENT)
    assert all(json.loads(item.kwargs["input"])["kind"] != "StatefulSet" for item in kube.call.call_args_list)


def test_controller_creation_race_during_admission_accepts_only_its_owned_pod():
    resources = manifests("default", IDENT, Path("/worktree/test"), "small", 600)
    controller = existing_controller(resources)
    pod = engine_pod_manifest(controller)
    reads = iter([{}, pod])
    kube = Kube("test-context", "default")
    kube.get = Mock(side_effect=lambda kind, name: controller if kind == "StatefulSet" else
                    next(reads) if kind == "pod" and name == engine_pod_name(IDENT) else {})
    kube.call = Mock(side_effect=PodgroveError("AlreadyExists"))
    kube.check_admission(resources)
    kube.call.assert_called_once()
    submitted = json.loads(kube.call.call_args.kwargs["input"])
    assert submitted["metadata"]["ownerReferences"][0]["uid"] == controller["metadata"]["uid"]


def test_foreground_controller_failure_prevents_storage_deletion():
    kube = Kube("test-context", "default")
    kube.get = Mock(side_effect=lambda kind, name: {"metadata": {"name": "default"}} if kind == "namespace" else {})
    kube.call = Mock(side_effect=PodgroveError("Pod did not terminate"))
    with pytest.raises(PodgroveError, match="did not terminate"):
        kube.destroy(IDENT)
    assert kube.call.call_count == 1
    args = kube.call.call_args.args
    assert args[1] == "statefulset"
    assert "--cascade=foreground" in args and "--wait=true" in args and "--force" not in args


@pytest.mark.parametrize("change", ["capacity", "class", "access", "mode", "deleting"])
def test_existing_pvc_drift_or_deletion_refused_before_any_resource_write(change):
    resources = manifests("default", IDENT, Path("/worktree/test"), "small", 600, storage_class="gp3")
    pvc = copy.deepcopy(next(item for item in resources if item["kind"] == "PersistentVolumeClaim"))
    if change == "capacity":
        pvc["spec"]["resources"]["requests"]["storage"] = "1Gi"
    elif change == "class":
        pvc["spec"]["storageClassName"] = "foreign-class"
    elif change == "access":
        pvc["spec"]["accessModes"] = ["ReadWriteMany"]
    elif change == "mode":
        pvc["spec"]["volumeMode"] = "Block"
    else:
        pvc["metadata"]["deletionTimestamp"] = "2026-09-25T10:35:00Z"
    kube = Kube("test-context", "default")
    kube.get = Mock(side_effect=lambda kind, name: pvc if kind == "PersistentVolumeClaim" else {})
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="different storage settings"):
        kube.create_environment(resources, IDENT)
    assert all("--dry-run=server" in item.args for item in kube.call.call_args_list)


@pytest.mark.parametrize("actual_size", ["20Gi", "20480Mi", "21474836480", "2147483648e1"])
def test_existing_pvc_accepts_equivalent_capacity_and_default_storage_class(actual_size):
    resource = next(item for item in manifests("default", IDENT, Path("/worktree/test"), "small", 600)
                    if item["kind"] == "PersistentVolumeClaim")
    existing = copy.deepcopy(resource)
    existing["spec"]["resources"]["requests"]["storage"] = actual_size
    existing["spec"]["storageClassName"] = "gp3"
    existing["spec"]["volumeMode"] = "Filesystem"
    kube = Kube("test-context", "default")
    kube.get = Mock(return_value=existing)
    kube.call = Mock()
    with_delete_storage(kube)
    kube.create_environment([resource], IDENT)
    kube.call.assert_not_called()


@pytest.mark.parametrize("mode", ["shared", "tainted"])
def test_existing_owned_engine_reconnect_skips_new_pod_admission(mode):
    resources = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600, node_mode=mode)
    controller = existing_controller(resources)
    pod = engine_pod_manifest(controller)
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(side_effect=lambda kind, name: controller if kind == "StatefulSet" else
                    pod if kind == "pod" and name == engine_pod_name(IDENT) else {})
    kube.call = Mock()
    with_delete_storage(kube)
    kube.create_environment(resources, IDENT)
    assert all("--dry-run=server" not in call.args for call in kube.call.call_args_list)
    assert not any(json.loads(call.kwargs["input"])["kind"] == "Pod" for call in kube.call.call_args_list)
    assert len(kube.call.call_args_list) == 4
    assert not any(json.loads(call.kwargs["input"])["kind"] == "StatefulSet" for call in kube.call.call_args_list)


def test_existing_unowned_engine_refused_before_any_other_environment_write():
    resources = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600)
    pod = copy.deepcopy(pod_from(resources))
    pod["metadata"]["labels"][ENVIRONMENT] = "different-owner"
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(side_effect=lambda kind, name: pod if kind == "pod" and name == engine_pod_name(IDENT) else {})
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="Refusing to modify unowned Pod"):
        kube.create_environment(resources, IDENT)
    kube.call.assert_not_called()


def test_existing_incompatible_engine_refused_before_any_supporting_object_update():
    resources = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600, node_mode="tainted")
    shared = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600)
    controller = existing_controller(shared)
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(side_effect=lambda kind, *_: controller if kind == "StatefulSet" else {})
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="different engine settings"):
        kube.create_environment(resources, IDENT)
    kube.call.assert_not_called()


def test_heartbeat_refuses_lease_replaced_by_another_owner():
    kube = Kube("test-context", "podgrove-testing")
    kube.call = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(
        {"metadata": {"labels": {MANAGED: "someone-else", ENVIRONMENT: IDENT}}})))
    with pytest.raises(PodgroveError, match="no longer owned"):
        kube.heartbeat(IDENT, 1234)
    assert kube.call.call_count == 1 and kube.call.call_args.args[0] == "get"


def test_manifest_has_dedicated_schedule_no_host_mounts_and_api_loopback():
    resources = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600, node_mode="tainted")
    pod = pod_from(resources)
    spec = pod["spec"]
    assert spec["nodeSelector"][DEDICATED] == "true"
    assert {"key": "dedicated", "operator": "Equal", "value": "podgrove", "effect": "NoSchedule"} in spec["tolerations"]
    assert not spec["automountServiceAccountToken"]
    assert not any("hostPath" in volume for volume in spec["volumes"])
    assert "--host=tcp://127.0.0.1:2375" in spec["containers"][0]["args"]
    policy = next(item for item in resources if item["kind"] == "NetworkPolicy")
    assert policy["spec"]["ingress"] == []
    assert policy["spec"]["podSelector"]["matchLabels"][ENVIRONMENT] == IDENT


def test_shared_preflight_does_not_require_any_node_access():
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(side_effect=AssertionError("Shared mode must not list nodes"))
    kube.call = Mock(return_value=SimpleNamespace(returncode=0, stdout="yes"))
    kube.preflight()
    kube.get.assert_not_called()
    assert all(call.args[0] == "auth" for call in kube.call.call_args_list)


def test_default_shared_manifest_obeys_existing_taints_and_excludes_unsupported_compute():
    resources = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600)
    pod = pod_from(resources)
    assert pod["metadata"]["labels"]["podgrove.dev/node-mode"] == "shared"
    assert pod["spec"]["nodeSelector"] == {"kubernetes.io/os": "linux"}
    assert pod["spec"]["tolerations"] == []
    terms = pod["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert terms == [{"matchExpressions": [{"key": "eks.amazonaws.com/compute-type", "operator": "NotIn", "values": ["fargate", "auto"]}]}]
    assert pod["spec"]["containers"][0]["resources"]["limits"]["memory"] == "2Gi"


@pytest.mark.parametrize("mode", ["shared", "tainted"])
@pytest.mark.parametrize("size,memory,cpu_request,cpu_limit", [
    ("small", "2Gi", "250m", "2"),
    ("medium", "8Gi", "1", "4"),
    ("large", "16Gi", "2", "8"),
])
def test_engine_reserves_full_memory_budget_before_scheduling(size, memory, cpu_request, cpu_limit, mode):
    resources = manifests("default", IDENT, Path("/worktree/test"), size, 600, node_mode=mode)
    pod = pod_from(resources)
    budget = pod["spec"]["containers"][0]["resources"]
    assert budget["requests"]["memory"] == budget["limits"]["memory"] == memory
    assert budget["requests"]["cpu"] == cpu_request
    assert budget["limits"]["cpu"] == cpu_limit


def test_invalid_node_mode_refused_before_cluster_access():
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock()
    kube.call = Mock()
    with pytest.raises(PodgroveError, match="node_mode"):
        kube.preflight(node_mode="typo")
    kube.get.assert_not_called()
    kube.call.assert_not_called()


def custom_placement():
    return {"selector": {"example.com/pool": "development"},
            "taint": {"key": "example.com/workload", "value": "sandbox", "effect": "NoExecute"}}


def test_custom_taint_selector_and_effect_drive_preflight_and_scheduling():
    placement = custom_placement()
    candidate = node()
    candidate["metadata"]["labels"] = {"example.com/pool": "development", "kubernetes.io/os": "linux"}
    candidate["spec"]["taints"] = [placement["taint"].copy()]
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(return_value={"items": [candidate]})
    kube.call = Mock(return_value=SimpleNamespace(returncode=0, stdout="yes"))
    kube.preflight(node_mode="tainted", tainted_nodes=placement)
    kube.get.assert_not_called()
    pod = pod_from(manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600,
                                   node_mode="tainted", tainted_nodes=placement))
    assert pod["spec"]["nodeSelector"] == {"kubernetes.io/os": "linux", "example.com/pool": "development"}
    assert pod["spec"]["tolerations"] == [{"operator": "Equal", **placement["taint"]}]


def test_soft_scheduling_taint_does_not_block_tainted_node_preflight():
    candidate = node()
    candidate["spec"]["taints"].append({"key": "prefer-other-workloads", "effect": "PreferNoSchedule"})
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(return_value={"items": [candidate]})
    kube.call = Mock(return_value=SimpleNamespace(returncode=0, stdout="yes"))
    kube.preflight(node_mode="tainted")
    assert kube.call.called


def test_empty_selector_value_uses_equality_and_matches_empty_taint_value():
    placement = {"selector": {"example.com/pool": ""},
                 "taint": {"key": "example.com/test-only", "value": "", "effect": "NoSchedule"}}
    candidate = node()
    candidate["metadata"]["labels"]["example.com/pool"] = ""
    # Kubernetes omits optional empty taint values from some serialized objects.
    candidate["spec"]["taints"] = [{"key": "example.com/test-only", "effect": "NoSchedule"}]
    kube = Kube("test-context", "podgrove-testing")
    kube.get = Mock(return_value={"items": [candidate]})
    kube.call = Mock(return_value=SimpleNamespace(returncode=0, stdout="yes"))
    kube.preflight(node_mode="tainted", tainted_nodes=placement)
    kube.get.assert_not_called()


@pytest.mark.parametrize("mode", ["shared", "tainted"])
def test_custom_selection_cannot_remove_mandatory_linux_compute_affinity(mode):
    resources = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600,
                          node_mode=mode, tainted_nodes=custom_placement())
    pod = pod_from(resources)
    assert pod["spec"]["nodeSelector"]["kubernetes.io/os"] == "linux"
    terms = pod["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert len(terms) == 1  # A second OR term could bypass the compute exclusions.
    assert {"key": "eks.amazonaws.com/compute-type", "operator": "NotIn", "values": ["fargate", "auto"]} in terms[0]["matchExpressions"]
    if mode == "shared":
        assert "example.com/pool" not in pod["spec"]["nodeSelector"]
        assert pod["spec"]["tolerations"] == []


@pytest.mark.parametrize("section,key,value", [
    ("selector", "example.com/pool", "comma,injection"),
    ("selector", "UpperCase.example/pool", "dev"),
    ("selector", "kubernetes.io/os", "windows"),
    ("selector", "eks.amazonaws.com/compute-type", "fargate"),
    ("selector", "eks.amazonaws.com/compute-type", "auto"),
    ("taint", "key", "invalid/key/again"),
    ("taint", "value", " " ),
    ("taint", "effect", "PreferNoSchedule"),
])
def test_custom_placement_syntax_rejected(section, key, value):
    placement = custom_placement()
    placement[section][key] = value
    with pytest.raises(PodgroveError, match="tainted_nodes"):
        validate_tainted_nodes(placement)


def test_taint_empty_value_is_valid_and_defaults_not_mutated():
    placement = custom_placement()
    placement["taint"]["value"] = ""
    validate_tainted_nodes(placement)
    first = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600, node_mode="tainted")
    pod_from(first)["spec"]["nodeSelector"][DEDICATED] = "changed"
    second = manifests("podgrove-testing", IDENT, Path("/worktree/test"), "small", 600, node_mode="tainted")
    assert pod_from(second)["spec"]["nodeSelector"][DEDICATED] == "true"


@pytest.mark.parametrize("root", ["/", "/etc", "/var/lib/docker/data", "/proc/self", "/run/test", "/usr/local/project"])
def test_manifest_refuses_mirror_that_overlays_engine_system_paths(root):
    with pytest.raises(PodgroveError, match="Unsafe worktree mirror"):
        manifests("podgrove-testing", IDENT, Path(root), "small", 600)


@pytest.fixture
def heartbeat_kube():
    kube = Kube("explicit-context", "team-dev", namespace_mode="shared")
    lease = {"metadata": {"name": "pg-" + IDENT, "namespace": "team-dev", "uid": "lease-uid",
                          "resourceVersion": "42", "labels": {MANAGED: "podgrove", ENVIRONMENT: IDENT}},
             "data": {"namespace_mode": "shared", "last_activity": "100"}}
    kube.call = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(lease)))
    return kube, lease


@pytest.mark.parametrize("stage", ["get", "replace"])
@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror"])
def test_heartbeat_transport_failure_is_typed_and_never_retries_stale_write(heartbeat_kube, stage, failure):
    kube, lease = heartbeat_kube
    def call(*args, **kwargs):
        assert kwargs["timeout"] == kube_module.REQUEST_PROCESS_TIMEOUT and kwargs["check"] is False
        if args[0] == stage:
            if failure == "exit":
                return SimpleNamespace(returncode=1, stdout="", stderr="private-credential-error")
            if failure == "timeout":
                raise PodgroveError("private-timeout-response")
            raise OSError("private-auth-error")
        return SimpleNamespace(returncode=0, stdout=json.dumps(lease))
    kube.call.side_effect = call
    with pytest.raises(kube_module.HeartbeatUnavailable) as error:
        kube.heartbeat(IDENT, 200)
    assert "private" not in str(error.value)
    assert [item.args[0] for item in kube.call.call_args_list] == (["get"] if stage == "get" else ["get", "replace"])
    if stage == "replace":
        submitted = json.loads(kube.call.call_args.kwargs["input"])
        assert submitted["metadata"]["uid"] == "lease-uid" and submitted["metadata"]["resourceVersion"] == "42"
        assert submitted["data"]["last_activity"] == "200"


def test_heartbeat_recovers_only_after_new_owned_lease_read(heartbeat_kube):
    kube, lease = heartbeat_kube
    newer = copy.deepcopy(lease)
    newer["metadata"]["resourceVersion"] = "43"
    kube.call.side_effect = [SimpleNamespace(returncode=0, stdout=json.dumps(lease)),
                             SimpleNamespace(returncode=1, stdout="", stderr="uncertain response"),
                             SimpleNamespace(returncode=0, stdout=json.dumps(newer)),
                             SimpleNamespace(returncode=0, stdout="{}")]
    with pytest.raises(kube_module.HeartbeatUnavailable):
        kube.heartbeat(IDENT, 200)
    kube.heartbeat(IDENT, 300)
    assert [item.args[0] for item in kube.call.call_args_list] == ["get", "replace", "get", "replace"]
    submitted = json.loads(kube.call.call_args.kwargs["input"])
    assert submitted["metadata"]["resourceVersion"] == "43" and submitted["data"]["last_activity"] == "300"


@pytest.mark.parametrize("change", ["missing", "foreign", "mode", "deleting", "version"])
def test_confirmed_lease_changes_are_fatal_and_never_written(heartbeat_kube, change):
    kube, lease = heartbeat_kube
    if change == "missing":
        lease = {}
    elif change == "foreign":
        lease["metadata"]["labels"][ENVIRONMENT] = "foreign"
    elif change == "mode":
        lease["data"]["namespace_mode"] = "exclusive"
    elif change == "deleting":
        lease["metadata"]["deletionTimestamp"] = "deleting"
    else:
        lease["metadata"].pop("resourceVersion")
    kube.call.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(lease))
    with pytest.raises(PodgroveError) as error:
        kube.heartbeat(IDENT, 200)
    assert not isinstance(error.value, kube_module.HeartbeatUnavailable)
    assert kube.call.call_count == 1
