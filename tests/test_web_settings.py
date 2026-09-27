"""Configuration-page reads are exact, bounded, sanitized and never mutations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from unittest.mock import Mock

import pytest
from podgrove.bootstrap import PROVISIONING_MARKER, render_bootstrap
from podgrove import web_settings as settings
from podgrove.web import WebError


def object_for(resource, **updates):
    body = {"apiVersion": "v1", "kind": resource.kind,
            "metadata": {"name": resource.name, "uid": "resource-uid", "creationTimestamp": "2026-09-25T00:00:00Z"}}
    if resource.namespace:
        body["metadata"]["namespace"] = resource.namespace
    if resource.kind.endswith("Binding"):
        body.update(roleRef={"kind": "Role", "apiGroup": "rbac.authorization.k8s.io", "name": "podgrove-client"},
                    subjects=[{"kind": "ServiceAccount", "name": "podgrove-client", "namespace": "podgrove-testing"}])
    body.update(updates)
    return body


def test_no_namespace_scope_never_calls_cluster_or_reads_environment():
    reader = Mock(side_effect=AssertionError("No read allowed"))
    result = settings.settings("explicit-context", [], read_command=reader)
    assert result["context"] == "explicit-context" and result["read_only"] is True
    assert result["selected_namespace"] is None and result["provisioning"] is None
    assert result["access"]["objects"] == [] and "--namespace" in result["warnings"][0]
    reader.assert_not_called()


@pytest.mark.parametrize("options,selected", [(["default"], "kube-system"), (["default"], "podgrove-testing"),
                                             ([], "default"), (["Bad_Namespace"], None), ([None], None),
                                             ([["default"]], None), (["default"] * 257, None)])
def test_scope_rejection_precedes_any_read(options, selected):
    reader = Mock(side_effect=AssertionError("No read allowed"))
    with pytest.raises(WebError) as failure:
        settings.settings("explicit-context", options, namespace=selected, read_command=reader)
    assert failure.value.status == 400
    reader.assert_not_called()


@pytest.mark.parametrize("context", [None, "", "   ", "x\nsecret", "x\x7fsecret", "x" * 513])
def test_context_validation_precedes_any_read(context):
    reader = Mock(side_effect=AssertionError("No read allowed"))
    with pytest.raises(WebError):
        settings.settings(context, ["default"], read_command=reader)
    reader.assert_not_called()


def test_only_exact_named_gets_are_used_and_missing_is_explicit():
    calls = []
    def read(command, **kwargs):
        calls.append((command, kwargs))
        return b""
    result = settings.settings("explicit-context", ["default", "podgrove-testing", "default"],
                               namespace="podgrove-testing", read_command=read)
    assert result["namespace_options"] == ["default", "podgrove-testing"]
    assert result["selected_namespace"] == "podgrove-testing"
    observed = [result["provisioning"], *result["access"]["objects"]]
    assert len(observed) == len(calls) == 8
    assert {row["status"] for row in observed} == {"missing"}
    expected = {(r.resource, r.name) for r in settings._resources("podgrove-testing")}
    for command, limits in calls:
        assert command[:5] == ["kubectl", "--context", "explicit-context", "--namespace", "podgrove-testing"]
        assert command[6:] == ["get", command[7], command[8], "-o", "json", "--ignore-not-found"]
        assert (command[7], command[8]) in expected
        assert limits["limit"] == 256 * 1024 and 0 < limits["timeout"] <= 6
        assert not {"apply", "create", "patch", "delete", "auth", "config", "secrets", "--all-namespaces"} & set(command)
    assert "effective permissions" in result["access"]["note"]
    assert result["bootstrap"]["status"] == "reference_only"
    assert result["namespace"] is None


@pytest.mark.parametrize("namespace", ["default", "team-development", "wt-explicit-shared", "a" * 63])
def test_every_catalog_resource_is_an_exact_namespaced_bootstrap_object(namespace):
    resources = settings._resources(namespace)
    assert len(resources) == 8
    assert all(item.namespace == namespace for item in resources)
    assert {item.kind for item in resources} == {"ConfigMap", "ServiceAccount", "Role", "RoleBinding"}
    assert len({(item.kind, item.name, item.namespace) for item in resources}) == len(resources)
    assert {(item.kind, item.name, item.namespace) for item in resources}.isdisjoint(
        (item.kind, item.name, item.namespace) for item in settings._resources("another-team"))


def test_custom_namespace_performs_only_its_exact_named_catalog_reads():
    calls = []
    def read(command, **kwargs):
        calls.append(command)
        return b""
    result = settings.settings("portable-context", ["team-development"], read_command=read)
    assert result["namespace_options"] == ["team-development"]
    assert result["bootstrap"]["namespace"] == "team-development"
    assert "podgrove-testing" not in json.dumps(result)
    expected = {(r.resource, r.name) for r in settings._resources("team-development")}
    assert len(calls) == len(expected) == 8
    for command in calls:
        assert command[:5] == ["kubectl", "--context", "portable-context", "--namespace", "team-development"]
        assert command[6] == "get" and (command[7], command[8]) in expected
        assert "list" not in command and "--all-namespaces" not in command


@pytest.mark.parametrize("namespace", ["team-development", "worktree-012345abcdef", "a" * 63])
def test_known_access_names_match_actual_rendered_bootstrap(namespace):
    rendered = render_bootstrap(namespace)
    actual = {(body["kind"], body["metadata"]["name"], body["metadata"].get("namespace"))
              for documents in rendered.values() for body in documents}
    assert {(r.kind, r.name, r.namespace) for r in settings._resources(namespace)} <= actual


def test_provisioning_marker_exposes_only_validated_mode_and_identity():
    resource = settings.Resource("ConfigMap", "configmaps", PROVISIONING_MARKER, "default")
    body = object_for(resource, data={"version": "1", "namespace_mode": "worktree",
                                     "environment": "012345abcdef", "private": "secret-data"})
    body["metadata"].update(labels={"app.kubernetes.io/managed-by": "podgrove", "podgrove.dev/component": "bootstrap",
                                      "private": "secret-label"}, annotations={"credential": "secret-annotation"})
    result = settings._sanitize(resource, body)
    assert result["version"] == "1" and result["namespace_mode"] == "worktree"
    assert result["environment"] == "012345abcdef" and result["uid"] == "resource-uid"
    assert "secret-" not in json.dumps(result)
    assert "labels" not in result and "data" not in result


@pytest.mark.parametrize("change", [
    lambda body: body["metadata"]["labels"].update({"app.kubernetes.io/managed-by": "foreign"}),
    lambda body: body["metadata"]["labels"].update({"podgrove.dev/environment": "012345abcdef"}),
    lambda body: body["data"].update(version="2"),
    lambda body: body["data"].update(namespace_mode="exclusive"),
    lambda body: body["data"].update(environment="must-not-occur-in-shared-mode"),
    lambda body: body["data"].update(namespace_mode="worktree"),
])
def test_foreign_or_ambiguous_provisioning_marker_is_not_reported_present(change):
    resource = settings.Resource("ConfigMap", "configmaps", PROVISIONING_MARKER, "default")
    body = object_for(resource, data={"version": "1", "namespace_mode": "shared"})
    body["metadata"]["labels"] = {"app.kubernetes.io/managed-by": "podgrove", "podgrove.dev/component": "bootstrap"}
    change(body)
    with pytest.raises(ValueError):
        settings._sanitize(resource, body)


def test_sa_omits_tokens_pull_secrets_and_annotations():
    resource = settings.Resource("ServiceAccount", "serviceaccounts", "podgrove-client", "default")
    body = object_for(resource, automountServiceAccountToken=False, secrets=[{"name": "secret-token"}],
                      imagePullSecrets=[{"name": "secret-registry"}])
    body["metadata"]["annotations"] = {"secret-annotation": "credential"}
    result = settings._sanitize(resource, body)
    assert result["automount_service_account_token"] is False
    assert "secret-" not in json.dumps(result)


def test_roles_and_bindings_expose_declarations_without_following_references():
    resource = settings.Resource("Role", "roles.rbac.authorization.k8s.io", "podgrove-client", "default")
    role = settings._sanitize(resource, object_for(resource, rules=[{
        "apiGroups": [""], "resources": ["pods", "pods/log"], "verbs": ["get"],
        "resourceNames": ["owned-name"], "unrecognized": "secret-rule"}]))
    assert role["rules"] == [{"api_groups": [""], "resources": ["pods", "pods/log"], "verbs": ["get"],
                              "resource_names": ["owned-name"], "non_resource_urls": []}]
    resource = settings.Resource("RoleBinding", "rolebindings.rbac.authorization.k8s.io", "podgrove-client", "default")
    body = object_for(resource)
    body["roleRef"]["name"] = "unrelated-admin-role"
    result = settings._sanitize(resource, body)
    assert result["role_ref"]["name"] == "unrelated-admin-role"
    assert result["subjects"] == [{"kind": "ServiceAccount", "name": "podgrove-client", "namespace": "podgrove-testing"}]
    assert "unrelated-admin-role" not in {r.name for r in settings._resources("default")}
    assert "secret-" not in json.dumps(role)


@pytest.mark.parametrize("change", [lambda body: body.update(kind="Secret"),
                                    lambda body: body["metadata"].update(name="foreign"),
                                    lambda body: body["metadata"].update(namespace="foreign"),
                                    lambda body: body["metadata"].pop("uid"),
                                    lambda body: body.update(metadata=[]),
                                    lambda body: body.update(rules="secret-invalid")])
def test_unexpected_resource_identity_or_structure_is_rejected(change):
    resource = settings.Resource("Role", "roles.rbac.authorization.k8s.io", "podgrove-client", "default")
    body = object_for(resource)
    change(body)
    with pytest.raises(ValueError):
        settings._sanitize(resource, body)


@pytest.mark.parametrize("raw", [b"{}", b"[]", b"secret-invalid-json", b"x" * (settings.READ_LIMIT + 1),
                               b'{"kind":"Namespace","metadata":{"name":"default","uid":"id"},"status":[]}'])
def test_bad_responses_are_inaccessible_and_never_reported_as_missing(raw):
    result = settings.settings("explicit-context", ["default"], read_command=lambda *a, **k: raw)
    assert result["provisioning"]["status"] == "inaccessible"
    assert "secret-invalid-json" not in json.dumps(result)
    assert result["warnings"]


def test_read_errors_are_generic_without_raw_cli_payload():
    def read(*args, **kwargs):
        raise WebError("Forbidden credential=secret-token server=https://private-api.invalid")
    result = settings.settings("explicit-context", ["default"], read_command=read)
    assert all(item["status"] == "inaccessible" for item in [result["provisioning"], *result["access"]["objects"]])
    assert not any(value in json.dumps(result) for value in ("secret-token", "private-api", "Forbidden"))


def test_rules_are_bounded_and_truncation_is_explicit():
    resource = settings.Resource("Role", "roles.rbac.authorization.k8s.io", "podgrove-client", "default")
    rule = {field: ["x" * 256] * 32 for field in ("apiGroups", "resources", "resourceNames", "verbs", "nonResourceURLs")}
    result = settings._sanitize(resource, object_for(resource, rules=[rule] * 64))
    assert result["truncated"] is True and len(json.dumps(result).encode()) < settings.OBJECT_LIMIT + 32
    assert 0 < len(result["rules"]) < 32


def test_one_page_has_at_most_four_reads_and_joins_its_workers():
    lock, release, four_started = threading.Lock(), threading.Event(), threading.Event()
    counts = {"active": 0, "peak": 0}
    def read(*args, **kwargs):
        with lock:
            counts["active"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
            if counts["active"] == 4:
                four_started.set()
        try:
            assert release.wait(3)
            return b""
        finally:
            with lock:
                counts["active"] -= 1
    with ThreadPoolExecutor(max_workers=1) as owner:
        future = owner.submit(settings.settings, "explicit-context", ["default"], read_command=read)
        try:
            assert four_started.wait(3)
        finally:
            release.set()
        assert future.result(timeout=3)["provisioning"]["status"] == "missing"
    assert counts == {"active": 0, "peak": 4}


def test_shared_deadline_skips_queued_reads_and_leaves_no_inflight_tasks(monkeypatch):
    monkeypatch.setattr(settings, "READ_BUDGET", .04)
    calls = []
    def read(*args, **kwargs):
        calls.append(kwargs["timeout"])
        time.sleep(kwargs["timeout"])
        raise WebError("timeout")
    started = time.monotonic()
    result = settings.settings("explicit-context", ["podgrove-testing"], read_command=read)
    assert time.monotonic() - started < .5
    observed = [result["provisioning"], *result["access"]["objects"]]
    assert len(calls) == 4 and all(0 < timeout <= .04 for timeout in calls)
    assert sum(row["status"] == "not_checked" for row in observed) == 4


def test_zero_budget_performs_no_reads(monkeypatch):
    monkeypatch.setattr(settings, "READ_BUDGET", 0)
    reader = Mock(side_effect=AssertionError("No read allowed"))
    result = settings.settings("explicit-context", ["default"], read_command=reader)
    assert result["provisioning"]["status"] == "not_checked"
    reader.assert_not_called()
