"""Portable worktree NetworkPolicy rules and additional infrastructure exclusions.

These are Kubernetes allow rules, not overriding denies. Administrators must
review other policies and their CNI's Service/NAT and local-node behavior.
"""
from __future__ import annotations

import ipaddress
import copy
import re

import jsonschema

from .errors import PodgroveError


PRIVATE_AND_SPECIAL_IPV4 = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)

MANAGED = "app.kubernetes.io/managed-by"
ENVIRONMENT = "podgrove.dev/environment"
NAMESPACE_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 63,
                    "pattern": r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"}
WORKTREE_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 128,
                   "pattern": r"^[^/\\\x00-\x1f\x7f]+$",
                   "description": "Case-sensitive glob matched against the stable worktree name shown in status, derived from the checkout directory, never its branch."}
PEER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["namespace"],
               "properties": {"namespace": NAMESPACE_SCHEMA, "worktree": WORKTREE_SCHEMA}}
NETWORK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "blocked_cidrs": {
            "type": "array", "maxItems": 128, "uniqueItems": True,
            "items": {"type": "string", "minLength": 3, "maxLength": 49},
            "description": "Additional infrastructure CIDRs excluded from public web egress; built-in exclusions always remain.",
        },
        "pod_to_pod": {"type": "string", "enum": ["disabled", "open", "selected"], "default": "disabled",
                       "description": "Disabled isolates engines; open permits all ports between Podgrove engines in any namespace; selected requires declared ingress and egress."},
        "expose": {
            "type": "array", "maxItems": 32, "uniqueItems": True,
            "items": {"type": "object", "additionalProperties": False, "required": ["service", "from"],
                      "properties": {
                          "service": {"type": "string", "minLength": 1, "maxLength": 128,
                                      "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"},
                          "from": {"type": "array", "minItems": 1, "maxItems": 32,
                                   "uniqueItems": True, "items": PEER_SCHEMA},
                      }},
        },
        "connect": {
            "type": "array", "maxItems": 32, "uniqueItems": True,
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["namespace", "worktree", "ports"],
                      "properties": {"namespace": NAMESPACE_SCHEMA, "worktree": WORKTREE_SCHEMA,
                                     "ports": {"type": "array", "minItems": 1, "maxItems": 128,
                                               "uniqueItems": True, "items": {
                                                   "type": "integer", "minimum": 1, "maximum": 65535,
                                                   "not": {"enum": [2375, 2376]}}}}},
        },
    },
    "allOf": [{"if": {"required": ["pod_to_pod"], "properties": {"pod_to_pod": {"const": "selected"}}},
               "else": {"not": {"anyOf": [{"required": ["expose"]}, {"required": ["connect"]}]}}}],
}


def network_settings(value: dict | None = None) -> dict:
    """Normalize network intent while keeping the disabled legacy default stable."""
    value = {} if value is None else value
    errors = list(jsonschema.Draft202012Validator(NETWORK_SCHEMA).iter_errors(value))
    if errors:
        error = errors[0]
        location = ".".join(["network", *(str(part) for part in error.absolute_path)])
        raise PodgroveError(f"{location}: {error.message}")
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
    settings = {"blocked_cidrs": result}
    mode = value.get("pod_to_pod", "disabled")
    if mode != "disabled":
        settings["pod_to_pod"] = mode
    if mode == "selected":
        settings["expose"] = copy.deepcopy(value.get("expose", []))
        settings["connect"] = copy.deepcopy(value.get("connect", []))
        services = set()
        for rule in settings["expose"]:
            if rule["service"] in services:
                raise PodgroveError("network.expose: duplicate service; combine its from rules")
            services.add(rule["service"])
        for peer in [*(peer for rule in settings["expose"] for peer in rule["from"]), *settings["connect"]]:
            if not re.fullmatch(NAMESPACE_SCHEMA["pattern"], peer["namespace"]):
                raise PodgroveError("network: namespace must be an exact Kubernetes namespace name")
            if "worktree" in peer and (not re.fullmatch(WORKTREE_SCHEMA["pattern"], peer["worktree"])
                                       or not peer["worktree"].strip() or peer["worktree"] in (".", "..")):
                raise PodgroveError("network: worktree must be a nonempty directory-name glob")
            if "ports" in peer and any(type(port) is not int for port in peer["ports"]):
                raise PodgroveError("network.connect.ports: expected integer published TCP ports")
        namespaces = {peer["namespace"] for rule in settings["expose"] for peer in rule["from"]}
        namespaces.update(peer["namespace"] for peer in settings["connect"])
        if len(namespaces) > 32:
            raise PodgroveError("network: at most 32 distinct peer namespaces can be declared")
    return settings


def policy_spec(ident: str, network: dict | None = None, *, ingress: list[dict] | None = None,
                egress: list[dict] | None = None) -> dict:
    """Render one engine's policy; selected peers stay denied until resolved."""
    if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{12}", ident):
        raise PodgroveError("Invalid environment identity for network policy")
    settings = network_settings(network)
    exclusions = [ipaddress.ip_network(cidr) for cidr in (*PRIVATE_AND_SPECIAL_IPV4, *settings["blocked_cidrs"])]
    ipv4 = list(ipaddress.collapse_addresses(item for item in exclusions if item.version == 4))
    outgoing = [{"to": [{
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
    }], "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]
    if ipaddress.ip_network("0.0.0.0/0") not in ipv4:
        outgoing.append({"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [str(item) for item in ipv4]}}],
                         "ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 80}]})
    incoming = []
    mode = settings.get("pod_to_pod", "disabled")
    if mode == "open":
        peer = {"namespaceSelector": {}, "podSelector": {"matchLabels": {MANAGED: "podgrove"}}}
        incoming.append({"from": [copy.deepcopy(peer)]})
        outgoing.append({"to": [peer]})
    elif mode == "selected":
        incoming.extend(copy.deepcopy(ingress or []))
        outgoing.extend(copy.deepcopy(egress or []))
    return {"podSelector": {"matchLabels": {MANAGED: "podgrove", ENVIRONMENT: ident}},
            "policyTypes": ["Ingress", "Egress"], "ingress": incoming, "egress": outgoing}


def validate_model(network: dict | None, model: dict) -> list[dict]:
    """Validate local exposures offline and return deterministic published ports."""
    settings = network_settings(network)
    if settings.get("pod_to_pod") != "selected":
        return []
    services = model.get("services", {}) if isinstance(model, dict) else {}
    if not isinstance(services, dict):
        raise PodgroveError("network.expose: expected normalized Compose services")
    result = []
    for rule in settings["expose"]:
        name = rule["service"]
        key = f"network.expose service {name}"
        service = services.get(name)
        if not isinstance(service, dict):
            raise PodgroveError(f"{key}: service is not active in this Compose project")
        deploy = service.get("deploy", {})
        if not isinstance(deploy, dict):
            raise PodgroveError(f"{key}: expected normalized Compose deployment settings")
        replicas = service.get("scale", deploy.get("replicas", 1))
        if type(replicas) is not int or replicas != 1:
            raise PodgroveError(f"{key}: exposure requires exactly one service replica")
        ports = service.get("ports", [])
        if not isinstance(ports, list) or not ports:
            raise PodgroveError(f"{key}: publish at least one TCP port on 0.0.0.0 first; expose alone is insufficient")
        targets = set()
        for port in ports:
            if (not isinstance(port, dict) or port.get("protocol", "tcp") != "tcp"
                    or port.get("host_ip", "0.0.0.0") not in ("0.0.0.0", "")):
                raise PodgroveError(f"{key}: every published port must use 0.0.0.0 TCP")
            try:
                target = port["target"]
                if type(target) is not int or not 1 <= target <= 65535:
                    raise ValueError
                value = port.get("published", 0)
                if isinstance(value, bool) or not isinstance(value, (int, str)):
                    raise ValueError
                if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
                    raise ValueError
                published = int(value)
                if not 1 <= published <= 65535:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise PodgroveError(f"{key}: declare a valid target and one stable published TCP port; target-only, zero and ranges cannot be exposed") from exc
            if published in (2375, 2376):
                raise PodgroveError(f"{key}: published ports 2375 and 2376 are reserved for Docker")
            if target in targets:
                raise PodgroveError(f"{key}: a target port has ambiguous multiple publications")
            targets.add(target)
            result.append({"service": name, "target": target, "published": published})
            if len(result) > 128:
                raise PodgroveError("network.expose: at most 128 published TCP ports can be exposed")
    return result
