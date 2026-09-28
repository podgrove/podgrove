"""Independent evaluation of rendered Kubernetes NetworkPolicy intent.

This models selector/port/CIDR rules and their additive union, not CNI packet
processing. It cannot prove enforcement, DNAT ordering, node-local/self traffic
behavior, DNS proxy behavior, or safety against a privileged workload escape.
"""
from __future__ import annotations

import copy
import ipaddress
from pathlib import Path
from unittest.mock import Mock

import pytest

from podgrove.bootstrap import render_bootstrap
from podgrove.kube import ENVIRONMENT, MANAGED, Kube, manifests, resolve_namespace


IDENT = "012345abcdef"
OTHER_IDENT = "abcdef012345"
ROOT = Path("/worktree/network-test")


def endpoint(namespace, address, labels=None):
    return {"namespace": namespace, "address": address, "labels": dict(labels or {})}


def matches(selector, labels):
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for expression in selector.get("matchExpressions", []):
        key, operator = expression["key"], expression["operator"]
        values = expression.get("values", [])
        accepted = {
            "In": key in labels and labels[key] in values,
            "NotIn": key not in labels or labels[key] not in values,
            "Exists": key in labels,
            "DoesNotExist": key not in labels,
        }
        if operator not in accepted:
            raise AssertionError(f"Unknown selector operator {operator}")
        if not accepted[operator]:
            return False
    return True


def peer_matches(peer, other, policy_namespace):
    if "ipBlock" in peer:
        assert set(peer) == {"ipBlock"}, "ipBlock cannot be combined with endpoint selectors"
        address = ipaddress.ip_address(other["address"])
        block = peer["ipBlock"]
        return address in ipaddress.ip_network(block["cidr"]) and not any(
            address in ipaddress.ip_network(excluded) for excluded in block.get("except", []))
    if "namespaceSelector" in peer:
        if other["namespace"] is None:
            return False
        namespace_labels = ({"kubernetes.io/metadata.name": other["namespace"]}
                            if other["namespace"] is not None else {})
        if not matches(peer["namespaceSelector"], namespace_labels):
            return False
    elif "podSelector" in peer and other["namespace"] != policy_namespace:
        return False
    if "podSelector" in peer:
        return other["namespace"] is not None and matches(peer["podSelector"], other["labels"])
    return True


def port_matches(rule, port, protocol):
    choices = rule.get("ports", [])
    if not choices:
        return True
    for choice in choices:
        if choice.get("protocol", "TCP") != protocol:
            continue
        if "port" not in choice:
            return True
        assert isinstance(choice["port"], int), "Named ports need endpoint definitions outside this evaluator"
        if choice["port"] <= port <= choice.get("endPort", choice["port"]):
            return True
    return False


def direction_allowed(policies, selected, other, direction, port, protocol="TCP"):
    selected_policies = []
    for policy in policies:
        if policy.get("kind") != "NetworkPolicy" or policy["metadata"]["namespace"] != selected["namespace"]:
            continue
        spec = policy["spec"]
        defaults = ["Ingress", *(["Egress"] if spec.get("egress") else [])]
        if direction in spec.get("policyTypes", defaults) and matches(spec["podSelector"], selected["labels"]):
            selected_policies.append(policy)
    if not selected_policies:
        return True
    peer_key = "to" if direction == "Egress" else "from"
    for policy in selected_policies:
        for rule in policy["spec"].get(direction.lower(), []):
            peers = rule.get(peer_key, [])
            if port_matches(rule, port, protocol) and (
                    not peers or any(peer_matches(peer, other, policy["metadata"]["namespace"]) for peer in peers)):
                return True
    return False


def connection_allowed(policies, source, destination, port, protocol="TCP"):
    return (direction_allowed(policies, source, destination, "Egress", port, protocol)
            and direction_allowed(policies, destination, source, "Ingress", port, protocol))


def rendered(mode="shared", *, network=None):
    namespace = resolve_namespace("team-network", mode, IDENT)
    documents = render_bootstrap(namespace, "delete-sc", namespace_mode=mode,
                                 identity=IDENT if mode == "worktree" else None)
    bootstrap = [document for group in documents.values() for document in group]
    resources = manifests(namespace, IDENT, ROOT, "small", 600, namespace_mode=mode, network=network)
    policies = [document for document in [*bootstrap, *resources] if document["kind"] == "NetworkPolicy"]
    engine = endpoint(namespace, "10.20.1.2", {MANAGED: "podgrove", ENVIRONMENT: IDENT})
    return namespace, bootstrap, resources, policies, engine


@pytest.fixture(params=["shared", "worktree"])
def environment(request):
    return rendered(request.param)


def test_bootstrap_baseline_selects_only_podgrove_pods_without_an_egress_grant(environment):
    namespace, bootstrap, _, _, _ = environment
    baseline = [item for item in bootstrap if item["kind"] == "NetworkPolicy"]
    assert baseline, "Applying bootstrap must establish isolation before any engine exists"
    for policy in baseline:
        assert policy["apiVersion"] == "networking.k8s.io/v1"
        assert policy["metadata"]["namespace"] == namespace
        assert policy["spec"]["podSelector"] == {"matchLabels": {MANAGED: "podgrove"}}
        assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}
        assert not policy["spec"].get("ingress") and not policy["spec"].get("egress")
        assert ENVIRONMENT not in policy["metadata"].get("labels", {})


@pytest.mark.parametrize("labels", [{MANAGED: "podgrove"}, {MANAGED: "podgrove", ENVIRONMENT: OTHER_IDENT}])
def test_missing_or_changed_environment_label_does_not_bypass_podgrove_baseline(environment, labels):
    namespace, _, _, policies, _ = environment
    unlabeled = endpoint(namespace, "10.20.1.3", labels)
    outside = endpoint("other-namespace", "10.30.1.4")
    internet = endpoint(None, "1.1.1.1")
    assert not connection_allowed(policies, unlabeled, outside, 443)
    assert not connection_allowed(policies, unlabeled, internet, 443)
    assert not connection_allowed(policies, outside, unlabeled, 80)


@pytest.mark.parametrize("labels", [{}, {MANAGED: "another-application"}])
def test_bootstrap_does_not_change_unrelated_workloads_network_access(environment, labels):
    namespace, _, _, policies, _ = environment
    unrelated = endpoint(namespace, "10.20.1.3", labels)
    outside = endpoint("other-namespace", "10.30.1.4")
    assert connection_allowed(policies, unrelated, outside, 443)
    assert connection_allowed(policies, unrelated, endpoint(None, "1.1.1.1"), 443)
    assert connection_allowed(policies, outside, unrelated, 80)


@pytest.mark.parametrize("other_namespace", [None, "same", "other-namespace"])
@pytest.mark.parametrize("port", [80, 443, 8080, 2375, 27017, 6379])
def test_incoming_tcp_is_denied_from_same_namespace_other_namespace_and_internet(environment, other_namespace, port):
    namespace, _, _, policies, engine = environment
    source = endpoint(namespace if other_namespace == "same" else other_namespace,
                      "1.1.1.1" if other_namespace is None else "10.30.1.4",
                      {MANAGED: "podgrove", ENVIRONMENT: OTHER_IDENT})
    assert not direction_allowed(policies, engine, source, "Ingress", port)
    assert not connection_allowed(policies, source, engine, port)


@pytest.mark.parametrize("other_namespace", ["same", "other-namespace"])
def test_outgoing_to_routed_private_pods_and_service_addresses_is_denied(environment, other_namespace):
    namespace, _, _, policies, engine = environment
    for address in ("10.30.1.4", "172.20.1.10", "192.168.30.5", "100.100.1.2"):
        destination = endpoint(namespace if other_namespace == "same" else other_namespace, address,
                               {MANAGED: "podgrove", ENVIRONMENT: OTHER_IDENT})
        for port in (80, 443, 8080):
            # Egress itself must deny; a peer's ingress policy must not hide a leak.
            assert not direction_allowed(policies, engine, destination, "Egress", port)


@pytest.mark.parametrize("address", [
    "0.1.2.3", "10.1.2.3", "100.64.1.2", "100.127.255.254", "127.0.0.2",
    "169.254.169.254", "169.254.170.2", "172.16.0.1", "172.31.255.254", "192.0.0.1",
    "192.0.2.20", "192.88.99.1", "192.168.1.2", "198.18.0.1", "198.19.255.254", "198.51.100.20",
    "203.0.113.20", "224.0.0.1", "239.1.2.3", "240.0.0.1", "255.255.255.255",
    "::1", "fd00::1", "fe80::1", "2001:4860:4860::8888",
])
def test_public_web_rule_excludes_private_metadata_reserved_and_ipv6_addresses(environment, address):
    _, _, _, policies, engine = environment
    destination = endpoint(None, address)
    for port in (80, 443):
        assert not direction_allowed(policies, engine, destination, "Egress", port)


@pytest.mark.parametrize("address", ["1.1.1.1", "8.8.8.8", "142.250.1.1"])
def test_public_ipv4_http_https_are_allowed_for_builds(environment, address):
    _, _, _, policies, engine = environment
    destination = endpoint(None, address)
    for port in (80, 443):
        assert connection_allowed(policies, engine, destination, port)
    assert not connection_allowed(policies, engine, destination, 22)
    assert not connection_allowed(policies, engine, destination, 443, "UDP")
    assert not connection_allowed(policies, engine, destination, 53, "UDP")


@pytest.mark.parametrize("address", ["10.100.1.3", "fd00:10::53"])
def test_only_selected_cluster_dns_endpoints_receive_tcp_udp_53(environment, address):
    _, _, _, policies, engine = environment
    resolver = endpoint("kube-system", address, {"k8s-app": "kube-dns"})
    for protocol in ("TCP", "UDP"):
        assert connection_allowed(policies, engine, resolver, 53, protocol)
        for port in (80, 443, 9153):
            assert not connection_allowed(policies, engine, resolver, port, protocol)


@pytest.mark.parametrize("namespace,labels", [
    ("other-namespace", {"k8s-app": "kube-dns"}),
    ("kube-system", {"k8s-app": "not-dns"}),
    ("kube-system", {}),
    ("other-namespace", {}),
])
def test_dns_allowance_is_and_of_exact_namespace_label_pod_label_and_port(environment, namespace, labels):
    _, _, _, policies, engine = environment
    impostor = endpoint(namespace, "10.100.1.3", labels)
    for protocol in ("TCP", "UDP"):
        assert not connection_allowed(policies, engine, impostor, 53, protocol)


@pytest.mark.parametrize("mode", ["shared", "worktree"])
def test_configured_nonprivate_cluster_ranges_close_public_ip_egress_without_losing_build_access(mode):
    _, _, _, policies, engine = rendered(mode, network={"blocked_cidrs": ["11.20.0.0/16", "44.30.2.0/24"]})
    for address in ("11.20.0.1", "11.20.255.254", "44.30.2.1", "44.30.2.254"):
        for port in (80, 443):
            assert not direction_allowed(policies, engine, endpoint("other-namespace", address), "Egress", port)
    for address in ("1.1.1.1", "11.21.0.1", "44.30.3.1"):
        assert connection_allowed(policies, engine, endpoint(None, address), 443)
    # Custom exclusions are additive; they cannot remove built-in private fences.
    for address in ("10.1.2.3", "169.254.169.254", "192.168.1.2", "198.18.0.1"):
        assert not direction_allowed(policies, engine, endpoint("other-namespace", address), "Egress", 443)


def test_standard_policies_are_additive_and_not_an_unoverridable_security_boundary():
    namespace, _, _, policies, engine = rendered()
    victim = endpoint("other-namespace", "10.50.1.3")
    assert not connection_allowed(policies, engine, victim, 443)
    unrelated_allow = {"kind": "NetworkPolicy", "metadata": {"namespace": namespace}, "spec": {
        "podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]}}
    assert connection_allowed([*policies, unrelated_allow], engine, victim, 443)


def test_public_ingress_or_unknown_public_cluster_addresses_require_operator_exclusions():
    _, _, _, policies, engine = rendered()
    # Namespace metadata cannot negate an ipBlock rule. This deliberately
    # documents the limitation rather than asserting a false universal deny.
    assert direction_allowed(policies, engine, endpoint("other-namespace", "11.20.1.4"), "Egress", 443)


@pytest.mark.parametrize("mode", ["shared", "worktree"])
def test_blocking_all_public_ipv4_preserves_only_the_explicit_dns_exception(mode):
    _, _, _, policies, engine = rendered(mode, network={"blocked_cidrs": ["0.0.0.0/0"]})
    for address in ("1.1.1.1", "8.8.8.8", "11.20.1.4"):
        for port in (80, 443):
            assert not connection_allowed(policies, engine, endpoint(None, address), port)
    resolver = endpoint("kube-system", "10.100.1.3", {"k8s-app": "kube-dns"})
    assert connection_allowed(policies, engine, resolver, 53, "UDP")
    assert connection_allowed(policies, engine, resolver, 53, "TCP")


def test_ipv6_exclusions_are_not_inserted_into_an_ipv4_ipblock():
    _, _, _, policies, engine = rendered(network={"blocked_cidrs": ["2001:db8::/32", "11.20.0.0/16"]})
    for policy in policies:
        for rule in policy["spec"].get("egress", []):
            for peer in rule.get("to", []):
                if "ipBlock" in peer:
                    block = peer["ipBlock"]
                    cidr = ipaddress.ip_network(block["cidr"])
                    assert all(ipaddress.ip_network(excluded).subnet_of(cidr) for excluded in block.get("except", []))
    assert not connection_allowed(policies, engine, endpoint(None, "2001:db8::1"), 443)
    assert connection_allowed(policies, engine, endpoint(None, "1.1.1.1"), 443)


@pytest.mark.parametrize("mode", ["shared", "worktree"])
def test_actual_cleanup_retains_bootstrap_policy_and_namespace_but_removes_owned_engine_policy(mode):
    namespace, bootstrap, resources, _, _ = rendered(mode)
    remaining = copy.deepcopy([*bootstrap, *resources])
    baseline = [item for item in remaining if item["kind"] == "NetworkPolicy"
                and ENVIRONMENT not in item["metadata"].get("labels", {})]
    assert baseline
    kube = Kube("mock-context", namespace, namespace_mode=mode)
    def get(kind, name=None, **kwargs):
        canonical = {"configmap": "ConfigMap"}[kind]
        return next((item for item in remaining if item["kind"] == canonical and item["metadata"]["name"] == name), {})
    aliases = {"statefulset": "StatefulSet", "pod": "Pod", "pvc": "PersistentVolumeClaim",
               "configmap": "ConfigMap", "networkpolicy": "NetworkPolicy", "service": "Service", "poddisruptionbudget": "PodDisruptionBudget"}
    def delete(*args, **kwargs):
        assert args[0] == "delete" and args[1] != "namespace"
        assert "--all" not in args
        labels = dict(piece.split("=", 1) for piece in args[args.index("-l") + 1].split(","))
        kinds = {aliases[kind] for kind in args[1].split(",")}
        remaining[:] = [item for item in remaining if not (
            item["kind"] in kinds and matches({"matchLabels": labels}, item["metadata"].get("labels", {})))]
    kube.get = Mock(side_effect=get)
    kube.call = Mock(side_effect=delete)
    kube.destroy(IDENT)
    assert all(item in remaining for item in baseline)
    assert all(item in remaining for item in bootstrap)
    assert all(item["kind"] != "Namespace" for item in bootstrap)
    assert not any(item["kind"] == "NetworkPolicy" and item["metadata"].get("labels", {}).get(ENVIRONMENT) == IDENT
                   for item in remaining)
