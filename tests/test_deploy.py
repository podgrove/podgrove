"""Offline namespace-only installation and ownership fences."""
from pathlib import Path
import re

import pytest
import yaml

from podgrove.bootstrap import PROVISIONING_MARKER, generate_bootstrap, render_bootstrap
from podgrove.kube import ENVIRONMENT, MANAGED, Kube, engine_pod_manifest, manifests
from podgrove.web_settings import _resources

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
NAMESPACE = "team-development"
CLIENT = {"kind": "ServiceAccount", "name": "podgrove-client", "namespace": NAMESPACE}
HUMAN = {"kind": "Group", "name": "podgrove-developers", "apiGroup": "rbac.authorization.k8s.io"}
REAPER = {"kind": "ServiceAccount", "name": "podgrove-reaper", "namespace": NAMESPACE}


def documents(folder="bootstrap"):
    if folder == "bootstrap":
        return [item for values in render_bootstrap(NAMESPACE).values() for item in values]
    return [item for source in sorted((DEPLOY / folder).glob("*.yaml.example"))
            for item in yaml.safe_load_all(source.read_text())]


def allowed(items, subject, verb, resource, *, group="", namespace=None, name=None):
    """Evaluate the explicit RBAC subset used here, including binding scope."""
    roles = {(item["kind"], item["metadata"].get("namespace"), item["metadata"]["name"]): item
             for item in items if item["kind"] in ("Role", "ClusterRole")}
    for binding in items:
        if binding["kind"] not in ("RoleBinding", "ClusterRoleBinding") or subject not in binding["subjects"]:
            continue
        scoped = binding["kind"] == "RoleBinding"
        if scoped and binding["metadata"]["namespace"] != namespace:
            continue
        ref = binding["roleRef"]
        scope = binding["metadata"]["namespace"] if ref["kind"] == "Role" else None
        role = roles[ref["kind"], scope, ref["name"]]
        for rule in role["rules"]:
            if (verb in rule["verbs"] and group in rule["apiGroups"] and resource in rule["resources"]
                    and ("resourceNames" not in rule or name in rule["resourceNames"])):
                return True
    return False


def test_bootstrap_has_only_concrete_owned_namespaced_objects_and_closed_bindings(tmp_path):
    paths = generate_bootstrap(tmp_path / "install", namespace=NAMESPACE)
    assert len(paths) == 5 and all(path.suffix == ".yaml" for path in paths)
    items = [item for path in paths for item in yaml.safe_load_all(path.read_text())]
    assert len(items) == 9
    assert {item["kind"] for item in items} == {"ServiceAccount", "Role", "RoleBinding", "ConfigMap", "NetworkPolicy"}
    identities = {(item["kind"], item["metadata"]["namespace"], item["metadata"]["name"]) for item in items}
    assert len(identities) == len(items)
    for item in items:
        assert item["metadata"]["namespace"] == NAMESPACE
        assert ENVIRONMENT not in item["metadata"].get("labels", {})
        assert not re.search(r"REPLACE_|\$\{|<YOUR_|{{", yaml.safe_dump(item))
        for rule in item.get("rules", []):
            assert all("*" not in value for field in ("verbs", "apiGroups", "resources") for value in rule[field])
            assert not set(rule["resources"]) & {"namespaces", "nodes", "persistentvolumes", "storageclasses",
                                                  "clusterroles", "clusterrolebindings", "resourcequotas", "limitranges"}
        if item["kind"] == "RoleBinding":
            assert item["roleRef"]["kind"] == "Role"
            assert ("Role", NAMESPACE, item["roleRef"]["name"]) in identities
            for subject in item["subjects"]:
                if subject["kind"] == "ServiceAccount":
                    assert ("ServiceAccount", subject["namespace"], subject["name"]) in identities


@pytest.mark.parametrize("subject", [CLIENT, HUMAN], ids=["automation", "human"])
def test_client_workload_access_is_namespaced_without_cluster_or_credentials_access(subject):
    items = documents()
    for group, resource in (("", "pods"), ("apps", "statefulsets"), ("", "services"),
                            ("", "persistentvolumeclaims"), ("", "configmaps"),
                            ("networking.k8s.io", "networkpolicies")):
        for verb in ("get", "list", "watch", "create", "patch", "update", "delete"):
            assert allowed(items, subject, verb, resource, group=group, namespace=NAMESPACE)
            assert not allowed(items, subject, verb, resource, group=group, namespace="foreign-ci")
    for resource in ("pods/exec", "pods/portforward"):
        assert allowed(items, subject, "get", resource, namespace=NAMESPACE)
        assert allowed(items, subject, "create", resource, namespace=NAMESPACE)
    assert allowed(items, subject, "get", "pods/log", namespace=NAMESPACE)
    for group, resource in (("", "namespaces"), ("", "nodes"), ("", "persistentvolumes"),
                            ("storage.k8s.io", "storageclasses"), ("rbac.authorization.k8s.io", "clusterroles"),
                            ("rbac.authorization.k8s.io", "clusterrolebindings"), ("", "secrets"),
                            ("", "serviceaccounts/token")):
        for verb in ("get", "list", "watch", "create", "patch", "update", "delete"):
            assert not allowed(items, subject, verb, resource, group=group, namespace=NAMESPACE, name="anything")


@pytest.mark.parametrize("subject", [CLIENT, HUMAN], ids=["automation", "human"])
def test_dashboard_catalog_reads_only_eight_namespaced_objects(subject):
    items = documents()
    catalog = _resources(NAMESPACE)
    assert len(catalog) == 8 and all(row.namespace == NAMESPACE for row in catalog)
    for row in catalog:
        plural, _, group = row.resource.partition(".")
        assert allowed(items, subject, "get", plural, group=group, namespace=NAMESPACE, name=row.name)
        assert not allowed(items, subject, "get", plural, group=group, namespace="foreign-ci", name=row.name)
        if row.kind != "ConfigMap":
            for verb in ("create", "patch", "update", "delete", "bind", "escalate", "impersonate"):
                assert not allowed(items, subject, verb, plural, group=group, namespace=NAMESPACE, name=row.name)
            assert not allowed(items, subject, "get", plural, group=group, namespace=NAMESPACE, name="foreign")
    role, = [item for item in items if item["kind"] == "Role" and item["metadata"]["name"] == "podgrove-client"]
    expected = {(row.resource.partition(".")[2], row.resource.partition(".")[0], row.name)
                for row in catalog if row.kind != "ConfigMap"}
    actual = {(group, resource, name) for rule in role["rules"] if "resourceNames" in rule
              for group in rule["apiGroups"] for resource in rule["resources"] for name in rule["resourceNames"]}
    assert actual == expected


def test_reaper_has_only_namespaced_cleanup_rights():
    items = documents()
    for group, resource in (("", "pods"), ("apps", "statefulsets"), ("", "services"),
                            ("", "persistentvolumeclaims"), ("", "configmaps"),
                            ("networking.k8s.io", "networkpolicies")):
        for verb in ("get", "list", "watch", "delete"):
            assert allowed(items, REAPER, verb, resource, group=group, namespace=NAMESPACE)
            assert not allowed(items, REAPER, verb, resource, group=group, namespace="foreign-ci")
        for verb in ("create", "patch", "update"):
            assert not allowed(items, REAPER, verb, resource, group=group, namespace=NAMESPACE)
    for group, resource in (("", "namespaces"), ("", "nodes"), ("", "persistentvolumes"),
                            ("", "pods/exec"), ("", "pods/portforward"), ("", "pods/log"), ("", "secrets"),
                            ("storage.k8s.io", "storageclasses"), ("rbac.authorization.k8s.io", "roles")):
        for verb in ("get", "list", "create", "update", "delete"):
            assert not allowed(items, REAPER, verb, resource, group=group, namespace=NAMESPACE)


@pytest.mark.parametrize("mode", ["shared", "worktree"])
def test_managed_pod_default_deny_never_selects_unrelated_ci_pods(mode):
    files = render_bootstrap(NAMESPACE, namespace_mode=mode,
                             identity="012345abcdef" if mode == "worktree" else None)
    policy, = files["05-network-isolation.yaml"]
    assert policy == {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                      "metadata": {"name": "podgrove-default-deny", "namespace": NAMESPACE,
                                   "labels": {MANAGED: "podgrove"}},
                      "spec": {"podSelector": {"matchLabels": {MANAGED: "podgrove"}},
                               "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []}}
    selector = policy["spec"]["podSelector"]["matchLabels"]
    for labels in ({}, {"app": "existing-ci"}, {MANAGED: "foreign-platform"}):
        assert not all(labels.get(key) == value for key, value in selector.items())
    for labels in ({MANAGED: "podgrove", ENVIRONMENT: "012345abcdef"},
                   {MANAGED: "podgrove", "podgrove.dev/component": "reaper"}):
        assert all(labels.get(key) == value for key, value in selector.items())


@pytest.mark.parametrize("mode", ["shared", "worktree"])
def test_runtime_create_and_cleanup_leave_provisioning_marker_and_baseline(tmp_path, mode):
    from test_namespace_only_runtime import NamespaceOnlyAPI

    ident = "012345abcdef"
    bootstrap = [item for values in render_bootstrap(NAMESPACE, namespace_mode=mode,
                 identity=ident if mode == "worktree" else None).values() for item in values]
    baseline, = [item for item in bootstrap if item["kind"] == "NetworkPolicy"]
    marker, = [item for item in bootstrap if item["kind"] == "ConfigMap"]
    runtime = manifests(NAMESPACE, ident, tmp_path, "small", 3600, namespace_mode=mode)
    assert not {(item["kind"], item["metadata"]["name"]) for item in bootstrap} & {
        (item["kind"], item["metadata"]["name"]) for item in runtime}
    kube = Kube("offline-context", NAMESPACE, namespace_mode=mode)
    api = NamespaceOnlyAPI(mode, ident=ident, namespace=NAMESPACE, context=kube.context)
    for item in bootstrap + runtime:
        api.add(item)
    retained = {key: item for key, item in api.objects.items()
                if item["metadata"].get("labels", {}).get(ENVIRONMENT) != ident}
    kube.call = lambda *args, **kwargs: api(kube.command(*args), **kwargs)
    kube.destroy(ident)
    assert api.objects == retained
    assert ("NetworkPolicy", baseline["metadata"]["name"]) in api.objects
    assert ("ConfigMap", marker["metadata"]["name"]) in api.objects
    policy = next(item for item in runtime if item["kind"] == "NetworkPolicy")
    assert ("NetworkPolicy", policy["metadata"]["name"]) not in api.objects


def test_optional_reaper_is_namespaced_and_uses_its_projected_cleanup_identity():
    items = documents("reaper")
    assert {item["kind"] for item in items} == {"ConfigMap", "CronJob", "NetworkPolicy"}
    assert all(item["metadata"]["namespace"] == "PODGROVE_NAMESPACE" for item in items)
    job = next(item for item in items if item["kind"] == "CronJob")
    spec = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == REAPER["name"] and spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["fsGroup"] == spec["securityContext"]["runAsUser"] == 65532
    assert spec["securityContext"]["runAsNonRoot"] is True
    container, = spec["containers"]
    assert "REPLACE_WITH_YOUR_REGISTRY" in container["image"]
    assert container["args"] == ["reap", "--context", "podgrove-reaper", "--namespace", "PODGROVE_NAMESPACE", "--all", "--json"]
    volumes = {volume["name"]: volume for volume in spec["volumes"]}
    cfg = next(item for item in items if item["kind"] == "ConfigMap")
    config = yaml.safe_load(cfg["data"]["config"])
    assert config["users"] == [{"name": "podgrove-reaper", "user": {"tokenFile": "/var/run/podgrove/auth/token"}}]
    assert config["contexts"][0]["context"]["namespace"] == "PODGROVE_NAMESPACE"
    token, = [source["serviceAccountToken"] for source in volumes["credentials"]["projected"]["sources"]
              if "serviceAccountToken" in source]
    assert token == {"path": "token", "expirationSeconds": 3600}
    assert job["spec"]["concurrencyPolicy"] == "Forbid"
    assert job["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] < 300


def test_optional_reaper_policy_cannot_select_engines_or_grant_general_internet(tmp_path):
    items = documents("reaper")
    job = next(item for item in items if item["kind"] == "CronJob")
    policy = next(item for item in items if item["kind"] == "NetworkPolicy")
    labels = job["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert labels == {MANAGED: "podgrove", "podgrove.dev/component": "reaper"}
    assert ENVIRONMENT not in policy["metadata"]["labels"]
    assert policy["spec"]["podSelector"] == {"matchLabels": labels}
    assert policy["spec"]["ingress"] == []
    dns, api = policy["spec"]["egress"]
    assert dns == {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                           "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                   "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}
    assert api == {"to": [{"ipBlock": {"cidr": "REPLACE_WITH_REVIEWED_API_SERVER_CIDR"}}],
                   "ports": [{"protocol": "TCP", "port": "REPLACE_WITH_REVIEWED_API_SERVER_PORT"}]}
    resources = manifests(NAMESPACE, "012345abcdef", tmp_path, "small", 3600)
    engine = engine_pod_manifest(next(item for item in resources if item["kind"] == "StatefulSet"))
    assert not all(engine["metadata"]["labels"].get(key) == value for key, value in labels.items())


def test_namespace_rbac_is_not_a_label_enforced_hostile_tenant_boundary():
    items = documents()
    for subject in (CLIENT, HUMAN, REAPER):
        assert allowed(items, subject, "delete", "networkpolicies", group="networking.k8s.io",
                       namespace=NAMESPACE, name="podgrove-default-deny")
        assert allowed(items, subject, "delete", "configmaps", namespace=NAMESPACE, name=PROVISIONING_MARKER)
    assert allowed(items, CLIENT, "create", "networkpolicies", group="networking.k8s.io", namespace=NAMESPACE)
    assert not allowed(items, REAPER, "create", "networkpolicies", group="networking.k8s.io", namespace=NAMESPACE)
