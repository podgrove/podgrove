"""Portable worktree NetworkPolicy rules and additional infrastructure exclusions.

These are Kubernetes allow rules, not overriding denies. Administrators must
review other policies and their CNI's Service/NAT and local-node behavior.
"""
from __future__ import annotations

import ipaddress

from .errors import PodgroveError


PRIVATE_AND_SPECIAL_IPV4 = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)


def network_settings(value: dict | None = None) -> dict:
    """Validate extra CIDRs; they can never replace the built-in exclusions."""
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {"blocked_cidrs"}:
        raise PodgroveError("network: expected only blocked_cidrs")
    configured = value.get("blocked_cidrs", [])
    if not isinstance(configured, list) or len(configured) > 128:
        raise PodgroveError("network.blocked_cidrs: expected a list of at most 128 CIDRs")
    result = []
    for item in configured:
        try:
            if not isinstance(item, str) or "/" not in item or "%" in item:
                raise ValueError
            network = ipaddress.ip_network(item, strict=True)
        except ValueError as exc:
            raise PodgroveError(f"network.blocked_cidrs: invalid CIDR {item!r}; use an IPv4/IPv6 network with no host bits") from exc
        canonical = str(network)
        if canonical in result:
            raise PodgroveError(f"network.blocked_cidrs: duplicate CIDR {canonical}")
        result.append(canonical)
    return {"blocked_cidrs": result}


def policy_spec(ident: str, network: dict | None = None) -> dict:
    """Select one engine and allow only CoreDNS plus filtered public web egress."""
    settings = network_settings(network)
    exclusions = [ipaddress.ip_network(cidr) for cidr in (*PRIVATE_AND_SPECIAL_IPV4, *settings["blocked_cidrs"])]
    ipv4 = list(ipaddress.collapse_addresses(item for item in exclusions if item.version == 4))
    egress = [{"to": [{
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
    }], "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]
    if ipaddress.ip_network("0.0.0.0/0") not in ipv4:
        egress.append({"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [str(item) for item in ipv4]}}],
                       "ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 80}]})
    return {"podSelector": {"matchLabels": {"app.kubernetes.io/managed-by": "podgrove", "podgrove.dev/environment": ident}},
            "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": egress}
