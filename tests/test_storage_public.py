"""Namespace-only storage checks without administrator or cloud access."""
import copy
from pathlib import Path
from unittest.mock import Mock

import pytest

from podgrove.errors import PodgroveError
from podgrove.kube import Kube, engine_pod_manifest, manifests

IDENT = "123456abcdef"


@pytest.fixture
def storage():
    resources = manifests("default", IDENT, Path("/worktree/test"), "small", 600, storage_class="gp3")
    desired = next(item for item in resources if item["kind"] == "PersistentVolumeClaim")
    pvc = copy.deepcopy(desired)
    pvc["metadata"].update(uid="claim-uid")
    pvc["spec"]["volumeName"] = "pv-name"
    pv = {"metadata": {"name": "pv-name", "uid": "pv-uid"}, "spec": {
        "storageClassName": "gp3", "persistentVolumeReclaimPolicy": "Delete",
        "claimRef": {"uid": "claim-uid", "name": f"pg-{IDENT}", "namespace": "default"}}}
    sc = {"metadata": {"name": "gp3", "annotations": {"storageclass.kubernetes.io/is-default-class": "true"}},
          "reclaimPolicy": "Delete", "provisioner": "ebs.csi.aws.com"}
    kube = Kube("test-context", "default")
    objects = {"persistentvolumeclaim": {}, "persistentvolume": pv, "storageclass": sc, "storageclasses": {"items": [sc]}}
    kube.get = Mock(side_effect=lambda kind, name=None: objects[kind])
    kube.call = Mock()
    return kube, resources, desired, pvc, pv, sc, objects


def test_explicit_class_needs_only_owned_pvc_read_and_does_not_claim_delete(storage):
    kube, resources, _, _, _, _, _ = storage
    assert kube.check_storage(resources) == [{"pvc": f"pg-{IDENT}", "storage_class": "gp3",
        "phase": None, "reclaim_policy": None, "reclaim_policy_verified": False}]
    kube.get.assert_called_once_with("persistentvolumeclaim", f"pg-{IDENT}")
    kube.call.assert_not_called()


@pytest.mark.parametrize("value", [None, "", "Upper", "bad/abc", ".start", "a" * 64])
def test_new_pvc_requires_explicit_valid_class_without_default_discovery(storage, value):
    kube, resources, desired, _, _, _, _ = storage
    desired["spec"].pop("storageClassName")
    if value is not None:
        desired["spec"]["storageClassName"] = value
    with pytest.raises(PodgroveError, match="explicit administrator-approved"):
        kube.check_storage(resources)
    kube.get.assert_called_once_with("persistentvolumeclaim", f"pg-{IDENT}")
    kube.call.assert_not_called()


def test_reconnect_reuses_class_from_owned_pvc_without_reading_storageclass_or_pv(storage):
    kube, resources, desired, pvc, _, _, objects = storage
    desired["spec"].pop("storageClassName")
    pvc["status"] = {"phase": "Bound"}
    objects["persistentvolumeclaim"] = pvc
    original = copy.deepcopy(pvc)
    result = kube.check_storage(resources)[0]
    assert result["storage_class"] == "gp3" and result["phase"] == "Bound"
    assert result["reclaim_policy_verified"] is False and result["reclaim_policy"] is None
    assert desired["spec"]["storageClassName"] == "gp3" and pvc == original
    kube.get.assert_called_once_with("persistentvolumeclaim", f"pg-{IDENT}")
    kube.call.assert_not_called()


@pytest.mark.parametrize("drift", ["namespace", "name", "manager", "owner", "class", "deleting"])
def test_reconnect_refuses_changed_or_foreign_claim_without_writes(storage, drift):
    kube, resources, _, pvc, _, _, objects = storage
    objects["persistentvolumeclaim"] = pvc
    if drift in ("namespace", "name"):
        pvc["metadata"][drift] = "foreign"
    elif drift in ("manager", "owner"):
        pvc["metadata"]["labels"]["app.kubernetes.io/managed-by" if drift == "manager" else "podgrove.dev/environment"] = "foreign"
    elif drift == "class":
        pvc["spec"]["storageClassName"] = "different-class"
    else:
        pvc["metadata"]["deletionTimestamp"] = "now"
    original = copy.deepcopy(objects)
    with pytest.raises(PodgroveError):
        kube.check_storage(resources)
    assert objects == original
    kube.call.assert_not_called()


@pytest.mark.parametrize("change", ["name", "namespace", "manager", "owner"])
def test_desired_pvc_identity_must_be_exact_before_any_read(storage, change):
    kube, resources, desired, _, _, _, _ = storage
    if change in ("name", "namespace"):
        desired["metadata"][change] = "foreign"
    else:
        desired["metadata"]["labels"]["app.kubernetes.io/managed-by" if change == "manager" else "podgrove.dev/environment"] = "foreign"
    with pytest.raises(PodgroveError, match="exact namespaced"):
        kube.check_storage(resources)
    kube.get.assert_not_called()
    kube.call.assert_not_called()


def test_missing_explicit_class_refusal_precedes_real_resource_writes(storage):
    kube, resources, desired, _, _, _, objects = storage
    desired["spec"].pop("storageClassName")
    kube.check_admission = Mock()
    kube.get = Mock(side_effect=lambda kind, name=None: objects.get(kind, {}))
    with pytest.raises(PodgroveError, match="explicit administrator-approved"):
        kube.create_environment(resources, IDENT)
    kube.call.assert_not_called()


def test_engine_uid_environment_is_bound_to_downward_api():
    resources = manifests("default", IDENT, Path("/worktree/test"), "small", 600)
    pod = engine_pod_manifest(next(item for item in resources if item["kind"] == "StatefulSet"))
    env = pod["spec"]["containers"][0]["env"]
    assert next(item for item in env if item["name"] == "PODGROVE_POD_UID") == {
        "name": "PODGROVE_POD_UID", "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.uid"}}}
