import copy
import io
import json
from unittest.mock import Mock, call

import pytest

from podgrove import reaper
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube


@pytest.mark.parametrize("url", [
    "http://gitlab.com/group/repo/-/merge_requests/1",
    "https://gitlab.com.attacker.example/group/repo/-/merge_requests/1",
    "https://gitlab.com@attacker.example/group/repo/-/merge_requests/1",
    "https://gitlab.com/group/repo/-/merge_requests/0",
    "https://gitlab.com/group/repo/-/merge_requests/1?token=foo",
    "https://127.0.0.1/group/repo/-/merge_requests/1",
])
def test_merge_request_url_cannot_send_credentials_elsewhere(url):
    with pytest.raises(PodgroveError, match="mr-url"):
        reaper.mr_endpoint(url)


def test_merge_request_endpoint_encodes_project_path():
    assert reaper.mr_endpoint("https://gitlab.com/group/sub/repo/-/merge_requests/123") == (
        "https://gitlab.com/api/v4/projects/group%2Fsub%2Frepo/merge_requests/123"
    )


@pytest.mark.parametrize("metadata", [
    {}, {"last_activity": 100, "ttl_seconds": "nan"}, {"last_activity": 100, "ttl_seconds": "inf"},
    {"last_activity": 100, "ttl_seconds": 0}, {"last_activity": 1000, "ttl_seconds": 1},
    {"last_activity": "nan", "ttl_seconds": 1}, {"last_activity": 0, "ttl_seconds": 1},
])
def test_invalid_lifecycle_metadata_fails_closed(metadata):
    with pytest.raises(PodgroveError, match="refusing cleanup"):
        reaper.reason(metadata, now=200)


def test_expired_ttl_does_not_require_available_gitlab(monkeypatch):
    check = Mock(side_effect=PodgroveError("network unavailable"))
    monkeypatch.setattr(reaper, "mr_closed", check)
    assert reaper.reason({"last_activity": 100, "ttl_seconds": 50, "mr_url": "https://gitlab.com/x/y/-/merge_requests/1"},
                         now=200) == "idle TTL expired"
    check.assert_not_called()


@pytest.mark.parametrize("state,closed", [("merged", True), ("closed", True), ("opened", False)])
def test_mr_state_interpretation(monkeypatch, state, closed):
    opener = Mock()
    opener.open.return_value = io.StringIO(json.dumps({"state": state}))
    monkeypatch.setattr(reaper.urllib.request, "build_opener", lambda *_: opener)
    assert reaper.mr_closed("https://gitlab.com/x/y/-/merge_requests/1") is closed


@pytest.mark.parametrize("reply", ["[]", "null", "garbage"])
def test_malformed_mr_response_is_actionable(monkeypatch, reply):
    opener = Mock()
    opener.open.return_value = io.StringIO(reply)
    monkeypatch.setattr(reaper.urllib.request, "build_opener", lambda *_: opener)
    with pytest.raises(PodgroveError, match="verify merge-request"):
        reaper.mr_closed("https://gitlab.com/x/y/-/merge_requests/1")


def test_raw_transport_timeout_is_actionable(monkeypatch):
    opener = Mock()
    opener.open.side_effect = TimeoutError("timed out")
    monkeypatch.setattr(reaper.urllib.request, "build_opener", lambda *_: opener)
    with pytest.raises(PodgroveError, match="verify merge-request"):
        reaper.mr_closed("https://gitlab.com/x/y/-/merge_requests/1")


def lease():
    ident = "123456abcdef"
    return {"metadata": {"name": f"pg-{ident}", "resourceVersion": "100", "namespace": "default",
                         "labels": {ENVIRONMENT: ident, MANAGED: "podgrove"}},
            "data": {"last_activity": "100", "ttl_seconds": "1"}}


def test_reaper_refresh_race_retains_recently_active_environment():
    original = lease()
    refreshed = {**original, "metadata": {**original["metadata"], "resourceVersion": "101"}}
    kube = Mock(context="test-context", namespace="default")
    kube.get.side_effect = [{"items": [original]}, refreshed]
    assert reaper.reap(kube) == []
    kube.destroy.assert_not_called()


def test_reaper_dry_run_and_real_run_use_exact_identity():
    original = lease()
    kube = Mock(context="test-context", namespace="default")
    kube.get.return_value = {"items": [original]}
    result = reaper.reap(kube, dry_run=True)
    assert result == [{"identity": "123456abcdef", "reason": "idle TTL expired", "deleted": False}]
    kube.destroy.assert_not_called()
    kube.get.side_effect = [{"items": [original]}, original]
    assert reaper.reap(kube)[0]["deleted"] is True
    kube.destroy.assert_called_once_with("123456abcdef", namespace_mode="shared")


def test_reaper_ignores_configmap_with_mismatched_owner_name():
    original = lease()
    original["metadata"]["name"] = "unrelated-configuration"
    kube = Mock(context="test-context", namespace="default")
    kube.get.return_value = {"items": [original]}
    assert reaper.reap(kube) == []
    kube.destroy.assert_not_called()


@pytest.mark.parametrize("labels", [{ENVIRONMENT: "123456abcdef"},
                                    {MANAGED: "someone-else", ENVIRONMENT: "123456abcdef"}])
def test_reaper_ignores_foreign_leases_even_if_listing_returns_them(labels):
    foreign = lease()
    foreign["metadata"]["labels"] = labels
    foreign["data"] = {"invalid": "must not inspect foreign lifecycle data"}
    kube = Mock(context="test-context", namespace="default")
    kube.get.return_value = {"items": [foreign]}
    assert reaper.reap(kube) == []
    kube.destroy.assert_not_called()
    kube.get.assert_called_once_with("configmap", selector=f"{MANAGED}=podgrove")


def test_reaper_rechecks_lease_ownership_even_when_resource_version_matches():
    original = lease()
    changed = copy.deepcopy(original)
    changed["metadata"]["labels"][MANAGED] = "another-manager"
    kube = Mock(context="test-context", namespace="default")
    kube.get.side_effect = [{"items": [original]}, changed]
    assert reaper.reap(kube) == []
    kube.destroy.assert_not_called()


def test_default_reaper_removes_only_owned_environment_and_retains_namespace_and_foreign_leases():
    owned = lease()
    foreign = copy.deepcopy(owned)
    foreign["metadata"]["labels"][MANAGED] = "cluster-admin"
    foreign["data"] = {}
    namespace = {"metadata": {"name": "default", "labels": {"existing": "cluster-admin"}}}
    before = copy.deepcopy(namespace)
    kube = Kube("test-context", "default")
    def get(kind, name=None, **kwargs):
        if kind == "namespace":
            assert name == "default"
            return namespace
        if name is None:
            return {"items": [foreign, owned]}
        assert name == owned["metadata"]["name"]
        return owned
    kube.get = Mock(side_effect=get)
    kube.call = Mock()
    assert reaper.reap(kube) == [{"identity": "123456abcdef", "reason": "idle TTL expired", "deleted": True}]
    assert kube.call.call_args_list == [
        call("delete", "statefulset", "-l", f"{MANAGED}=podgrove,{ENVIRONMENT}=123456abcdef",
             "--ignore-not-found", "--cascade=foreground", "--wait=true", "--timeout=120s", "--request-timeout=0", timeout=130),
        call("delete", "pod,pvc,configmap,networkpolicy,service,poddisruptionbudget", "-l", f"{MANAGED}=podgrove,{ENVIRONMENT}=123456abcdef",
             "--ignore-not-found", "--wait=true", "--timeout=120s", timeout=130),
    ]
    assert namespace == before
