"""Exact TCP authorization, guarded discovery and connection lifecycle regressions."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from podgrove import connect
from podgrove.config import Config
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT, MANAGED, Kube
from podgrove.network import policy_spec

SOURCE, TARGET, THIRD = "a" * 12, "b" * 12, "c" * 12
RULE = {"name": "api", "environment": TARGET, "service": "gateway", "port": 8080}
ENDPOINT = {**RULE, "controller_uid": "target-controller", "published": 31888}


class MemoryKube(Kube):
    def __init__(self):
        super().__init__("test", "approved", namespace_mode="shared")
        self.objects = {}
        self.commands = []
        for ident in (SOURCE, TARGET):
            for kind in ("StatefulSet", "ConfigMap"):
                obj = {"apiVersion": "v1", "kind": kind, "data": {"compose_project": "api"}, "metadata": {
                    "name": "pg-" + ident, "namespace": self.namespace, "uid": ident + kind, "resourceVersion": "1",
                    "labels": {MANAGED: "podgrove", ENVIRONMENT: ident}}}
                self.objects[(kind.lower(), "pg-" + ident)] = obj

    def call(self, *args, **kwargs):
        self.commands.append((args, kwargs))
        if args[0] == "get":
            if "-l" in args:
                selector = dict(pair.split("=", 1) for pair in args[args.index("-l") + 1].split(","))
                result = {"items": [deepcopy(obj) for (kind, name), obj in self.objects.items()
                                    if kind == args[1].lower() and all(obj["metadata"]["labels"].get(key) == value
                                                                     for key, value in selector.items())]}
            else:
                result = deepcopy(self.objects.get((args[1].lower(), args[2]), {}))
        elif args[0] in ("create", "replace"):
            result = json.loads(kwargs["input"])
            metadata = result["metadata"]
            key = (result["kind"].lower(), metadata["name"])
            previous = self.objects.get(key)
            if previous:
                assert args[0] == "replace"
                assert metadata["uid"] == previous["metadata"]["uid"]
                assert metadata["resourceVersion"] == previous["metadata"]["resourceVersion"]
            else:
                assert args[0] == "create"
                metadata["uid"] = metadata["name"] + "-uid"
                if result["kind"] == "Service":
                    result["spec"]["clusterIP"] = "10.1.2.3"
            metadata["resourceVersion"] = str(int(metadata.get("resourceVersion", "0")) + 1)
            self.objects[key] = deepcopy(result)
        elif args[0] == "delete":
            assert args[1] == "--raw" and args[3:] == ("-f", "-")
            parts = args[2].split("/")
            assert parts[-4:-2] == ["namespaces", self.namespace]
            kind = {"networkpolicies": "networkpolicy", "services": "service"}[parts[-2]]
            key = (kind, parts[-1])
            metadata = self.objects[key]["metadata"]
            assert json.loads(kwargs["input"])["preconditions"] == {
                key: metadata[key] for key in ("uid", "resourceVersion")}
            del self.objects[key]
            result = {}
        else:
            raise AssertionError(args)
        return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")


def test_connection_policies_allow_exact_pair_port_and_nothing_else():
    service, outgoing, incoming = connect.link_resources("approved", SOURCE, "source-controller", ENDPOINT)
    assert service["spec"]["ports"] == [{"name": "tcp", "port": 8080, "targetPort": 31888, "protocol": "TCP"}]
    assert service["spec"]["selector"] == {MANAGED: "podgrove", ENVIRONMENT: TARGET}
    source = {"matchLabels": {MANAGED: "podgrove", ENVIRONMENT: SOURCE}}
    target = {"matchLabels": {MANAGED: "podgrove", ENVIRONMENT: TARGET}}
    assert outgoing["spec"] == {"podSelector": source, "policyTypes": ["Egress"],
                                "egress": [{"to": [{"podSelector": target}], "ports": [{"protocol": "TCP", "port": 31888}]}]}
    assert incoming["spec"] == {"podSelector": target, "policyTypes": ["Ingress"],
                                "ingress": [{"from": [{"podSelector": source}], "ports": [{"protocol": "TCP", "port": 31888}]}]}
    for obj in (service, outgoing, incoming):
        assert obj["metadata"]["namespace"] == "approved"
        assert obj["metadata"]["labels"][ENVIRONMENT] == SOURCE
        assert "namespaceSelector" not in json.dumps(obj)
        assert THIRD not in json.dumps(obj)
    assert policy_spec(TARGET)["ingress"] == []


def test_reconcile_preserves_service_ip_retargets_port_and_removes_undeclared_links(monkeypatch):
    kube = MemoryKube()
    endpoint = deepcopy(ENDPOINT)
    monkeypatch.setattr(connect, "discover_endpoint", lambda *_: deepcopy(endpoint))
    links = connect.EnvironmentLinks(kube, SOURCE, [RULE])
    links._reconcile()
    assert links.aliases == {"api.podgrove": "10.1.2.3"}
    endpoint["published"] = 31889
    links._reconcile()
    policies = links._existing("NetworkPolicy")
    assert len(policies) == 2
    assert all("31888" not in json.dumps(obj) for obj in policies)
    assert links.aliases == {"api.podgrove": "10.1.2.3"}
    links.rules = []
    links._reconcile()
    assert not links._existing("NetworkPolicy") and not links._existing("Service")
    assert kube.get("statefulset", "pg-" + TARGET)
    assert links.snapshot()["state"] == "disabled"


def test_foreign_connection_object_is_never_adopted(monkeypatch):
    kube = MemoryKube()
    monkeypatch.setattr(connect, "discover_endpoint", lambda *_: deepcopy(ENDPOINT))
    links = connect.EnvironmentLinks(kube, SOURCE, [RULE])
    links._reconcile()
    service = next(value for value in kube.objects.values() if value["kind"] == "Service")
    service["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    before = deepcopy(kube.objects)
    with pytest.raises(PodgroveError, match="foreign"):
        links._reconcile()
    assert kube.objects == before


def test_recreated_target_controller_is_refused(monkeypatch):
    kube = MemoryKube()
    endpoint = deepcopy(ENDPOINT)
    monkeypatch.setattr(connect, "discover_endpoint", lambda *_: deepcopy(endpoint))
    links = connect.EnvironmentLinks(kube, SOURCE, [RULE])
    links._reconcile()
    endpoint["controller_uid"] = "replacement-controller"
    with pytest.raises(PodgroveError, match="target controller changed"):
        links._reconcile()
    assert not links._existing("NetworkPolicy")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::", "192.168.1.1"])
def test_loopback_and_ambiguous_publishers_cannot_be_exposed(monkeypatch, host):
    kube = MemoryKube()
    tunnel = Mock(port=12345)
    monkeypatch.setattr(connect, "DockerTunnel", lambda *_: tunnel)
    cid = "d" * 64
    rows = [{"Id": cid, "State": "running", "Labels": {
        "com.docker.compose.service": "gateway", "com.docker.compose.project": "api"}}]
    detail = {"Id": cid, "Config": {"Labels": rows[0]["Labels"]}, "State": {"Running": True}, "NetworkSettings": {"Ports": {
        "8080/tcp": [{"HostIp": host, "HostPort": "31888"}]}}}
    monkeypatch.setattr(connect, "_docker_json", Mock(side_effect=[rows, detail]))
    with pytest.raises(PodgroveError, match="0.0.0.0 TCP"):
        connect.discover_endpoint(kube, RULE)
    tunnel.close.assert_called_once()


def test_discovery_checks_identity_after_reading_exact_running_service(monkeypatch):
    kube = MemoryKube()
    tunnel = Mock(port=12345)
    tunnel.identity_snapshot.return_value = {"expected": {"statefulset_uid": TARGET + "StatefulSet", "pod_uid": "pod"}}
    monkeypatch.setattr(connect, "DockerTunnel", lambda *_: tunnel)
    cid = "d" * 64
    rows = [{"Id": cid, "State": "running", "Labels": {
        "com.docker.compose.service": "gateway", "com.docker.compose.project": "api"}}]
    detail = {"Id": cid, "Config": {"Labels": rows[0]["Labels"]}, "State": {"Running": True}, "NetworkSettings": {"Ports": {
        "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "31888"}]}}}
    monkeypatch.setattr(connect, "_docker_json", Mock(side_effect=[rows, detail]))
    assert connect.discover_endpoint(kube, RULE)["published"] == 31888
    tunnel.refresh_identity.assert_called_once()
    tunnel.close.assert_called_once()


def test_self_link_alias_conflict_and_reverse_port_collision_are_refused(tmp_path):
    config = Config(tmp_path, [], connect=[{**RULE, "environment": SOURCE}])
    with pytest.raises(PodgroveError, match="another environment"):
        connect.validate_connectivity(config, {"services": {"web": {}}}, SOURCE)
    config.connect = [RULE]
    with pytest.raises(PodgroveError, match="alias api.podgrove"):
        connect.validate_connectivity(config, {"services": {"web": {"extra_hosts": {"api.podgrove": "1.2.3.4"}}}}, SOURCE)
    config.reverse = [{"remote_port": 8888, "local_port": 12345, "local_host": "127.0.0.1"}]
    with pytest.raises(PodgroveError, match="reverse.remote_port"):
        connect.validate_connectivity(config, {"services": {"web": {"ports": [{"published": "8800-8900"}]}}}, SOURCE)


def test_existing_host_gateway_mapping_and_unrelated_hosts_are_preserved(tmp_path):
    config = Config(tmp_path, [], reverse=[{"remote_port": 8888, "local_port": 12345}])
    model = {"services": {"web": {"extra_hosts": {"host.docker.internal": ["host-gateway"], "elsewhere": ["1.2.3.4"]}}}}
    before = deepcopy(model)
    connect.validate_connectivity(config, model, SOURCE)
    overlay = connect.overlay_model(model, {"host.docker.internal": "host-gateway"})
    assert overlay == {"services": {"web": {"extra_hosts": {"host.docker.internal": "host-gateway"}}}}
    assert model == before
