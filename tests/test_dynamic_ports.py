"""Resolve Docker-assigned ports, then refuse stale or ambiguous endpoints."""
from copy import deepcopy
import json
import shutil
import socket
from unittest.mock import Mock

import pytest

from podgrove import cli, runtime, state
from podgrove.compose import Compose
from podgrove.config import load_config
from podgrove.errors import PodgroveError
from podgrove.forward import PortMappingError, port_plan, validate_port_plan, verify_port_mappings
from test_forward_recovery import echo, wait_until
from test_runtime_recovery import session as session


IDENT = "123456abcdef"


def declaration(published=0, service="mongo", target=27017):
    return {"service": service, "target": target, "published": published, "protocol": "tcp"}


def observation(published=49155, service="mongo", target=27017):
    return {"ID": "a" * 64, "Project": "fixture", "Service": service, "State": "running",
            "Publishers": [{"URL": "0.0.0.0", "TargetPort": target,
                            "PublishedPort": published, "Protocol": "tcp"}]}


@pytest.mark.parametrize("syntax,declared", [('"27017"', 0), ('"0:27017"', 0),
                                           ('"49152-49160:27017"', "49152-49160"),
                                           ('{target: 27017}', 0),
                                           ('{target: 27017, published: "0"}', 0),
                                           ('{target: 27017, published: "49155"}', 49155)])
def test_real_compose_normalization_and_cli_validate_accept_dynamic_ports(tmp_path, monkeypatch, capsys,
                                                                       syntax, declared):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI required for normalization; no daemon is used")
    (tmp_path / "compose.yml").write_text(f'name: fixture\nservices:\n  mongo:\n    image: mongo:7\n    ports: [{syntax}]\n')
    (tmp_path / "podgrove.yml").write_text(
        "cluster: {context: offline-fixture, namespace: offline-owned}\ncompose: {files: [compose.yml]}\n")
    kube = Mock(side_effect=AssertionError("validation must not access Kubernetes"))
    monkeypatch.setattr(cli, "Kube", kube)
    compose = Compose(load_config(tmp_path))
    model = compose.model()
    ports = compose.published_ports(model)
    assert ports == [declaration(declared)]
    assert cli.execute(cli.parser().parse_args(["validate", "--project-directory", str(tmp_path)])) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    kube.assert_not_called()
    resolved = port_plan(ports, None, IDENT, observed=[observation()], project=model["name"])
    assert resolved[0]["published"] == 49155
    assert resolved[0]["target"] == 27017
    verify_port_mappings(resolved, [observation()], model["name"])


@pytest.mark.parametrize("declared", [0, "49152-49160"])
def test_dynamic_resolution_keeps_stable_local_preference_and_declaration(declared):
    raw = [declaration(declared)]
    validate_port_plan(raw, None, IDENT)
    with pytest.raises(PortMappingError, match="requires observed Docker publishers"):
        port_plan(raw, None, IDENT)
    first = port_plan(raw, None, IDENT, observed=[observation(49155)], project="fixture")
    second = port_plan(raw, None, IDENT, observed=[observation(49156)], project="fixture")
    assert first[0]["local"] == second[0]["local"]
    assert first[0]["declared_published"] == second[0]["declared_published"] == declared
    assert raw == [declaration(declared)]
    verify_port_mappings(first, [observation(49155)], "fixture")
    with pytest.raises(PortMappingError, match="reassigned.*up --refresh"):
        verify_port_mappings(first, [observation(49156)], "fixture")


def test_selected_dynamic_and_static_services_are_resolved_independently():
    rows = [observation(), observation(8080, "web", 80)]
    ports = port_plan([declaration(), declaration(8080, "web", 80)], None, IDENT,
                      observed=rows, project="fixture")
    assert [p["published"] for p in ports] == [49155, 8080]
    assert len({p["local"] for p in ports}) == 2
    assert "declared_published" not in ports[1]
    assert ports[1]["local"] == port_plan([declaration(8080, "web", 80)], None, IDENT)[0]["local"]


def test_explicit_local_preference_and_disabled_forwarding():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        local = reserved.getsockname()[1]
        rules = [{"service": "mongo", "port": 27017, "local": local}]
        with pytest.raises(PodgroveError, match="already in use"):
            validate_port_plan([declaration()], rules, IDENT)
    result = port_plan([declaration()], rules, IDENT, observed=[observation()], project="fixture")
    assert result[0]["local"] == local
    assert port_plan([declaration()], [], IDENT, observed=[], project="fixture") == []


def test_ipv4_and_ipv6_duplicate_publishers_have_one_remote_port():
    row = observation()
    row["Publishers"].append({**row["Publishers"][0], "URL": "::"})
    assert port_plan([declaration()], None, IDENT, observed=[row], project="fixture")[0]["published"] == 49155


@pytest.mark.parametrize("change", ["foreign-project", "missing-project", "absent-service", "exited",
                                   "replicas", "missing-publishers", "empty-publishers", "ambiguous",
                                   "foreign-bind", "wrong-protocol", "wrong-target", "reserved",
                                   "invalid-port", "string-port", "bool-port", "malformed-publisher"])
def test_unsafe_or_ambiguous_observations_never_create_a_forward(change):
    row = observation()
    rows = [row]
    binding = row["Publishers"][0]
    if change == "foreign-project":
        row["Project"] = "another-project"
    elif change == "missing-project":
        row.pop("Project")
    elif change == "absent-service":
        row["Service"] = "unrelated"
    elif change == "exited":
        row["State"] = "exited"
    elif change == "replicas":
        rows.append(deepcopy(row))
    elif change == "missing-publishers":
        row.pop("Publishers")
    elif change == "empty-publishers":
        row["Publishers"] = []
    elif change == "ambiguous":
        row["Publishers"].append({**binding, "PublishedPort": 49156})
    elif change == "foreign-bind":
        binding["URL"] = "192.0.2.1"
    elif change == "wrong-protocol":
        binding["Protocol"] = "udp"
    elif change == "wrong-target":
        binding["TargetPort"] = 8080
    elif change == "reserved":
        binding["PublishedPort"] = 2375
    elif change == "invalid-port":
        binding["PublishedPort"] = 0
    elif change == "string-port":
        binding["PublishedPort"] = "49155"
    elif change == "bool-port":
        binding["PublishedPort"] = True
    else:
        row["Publishers"] = [None]
    with pytest.raises(PortMappingError):
        port_plan([declaration()], None, IDENT, observed=rows, project="fixture")


@pytest.mark.parametrize("declared", [49154, "49156-49160"])
def test_observed_port_must_match_static_or_range_declaration(declared):
    with pytest.raises(PortMappingError, match="does not match"):
        port_plan([declaration(declared)], None, IDENT, observed=[observation()], project="fixture")


@pytest.mark.parametrize("declared", [2375, "2370-2380", "60000-40000", -1, True, "not-a-range"])
def test_invalid_or_reserved_declarations_fail_before_runtime(declared):
    with pytest.raises(PodgroveError):
        validate_port_plan([declaration(declared)], None, IDENT)


def test_duplicate_targets_and_udp_remain_unsupported():
    with pytest.raises(PodgroveError, match="exactly one"):
        validate_port_plan([declaration(), declaration(49155)], None, IDENT)
    with pytest.raises(PodgroveError, match="UDP"):
        validate_port_plan([{**declaration(), "protocol": "udp"}], None, IDENT)


@pytest.mark.parametrize("session", [{"published": 0}, {"published": "8000-8100"}], indirect=True)
def test_supervisor_resolves_dynamic_endpoint_before_starting_real_local_forward(session):
    current = state.read(session.path)
    port = current["ports"][0]
    assert port["published"] == 8080 and port["local"] == session.port and port["status"] == "ready"
    assert port["declared_published"] in (0, "8000-8100")
    assert session.tunnel.ports == [(session.port, 8080)]
    echo(session.port)
    assert runtime.control(session.data, "ping")["forward_status"]["state"] == "ready"
    assert all(call[1] in {"pod", "statefulset"} for call in session.kube.reads)


@pytest.mark.parametrize("session", [{"published": 0}], indirect=True)
@pytest.mark.parametrize("change", ["reassigned", "missing", "foreign-project"])
def test_recreation_stops_real_listener_but_keeps_engine_sync_and_control(session, monkeypatch, change):
    echo(session.port)
    if change == "reassigned":
        session.rows[0]["Publishers"][0]["PublishedPort"] = 8081
    elif change == "missing":
        session.rows[0]["Publishers"] = []
    else:
        session.rows[0]["Project"] = "another-project"
    monkeypatch.setattr(runtime, "HEALTH_INTERVAL", 0)
    runtime.control(session.data, "touch")
    wait_until(lambda: "up --refresh" in (state.read(session.path)["forward_status"].get("error") or ""))
    current = state.read(session.path)
    assert current["status"] == "degraded"
    assert current["ports"][0]["status"] == "disconnected"
    assert current["ports"][0]["local"] == session.port
    assert "up --refresh" in current["forward_status"]["error"]
    assert runtime.control(session.data, "ping")["forward_status"]["state"] == "disconnected"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", session.port), timeout=.2)
    session.source.write_text("source still synchronizes after port recreation")
    wait_until(lambda: bool(session.sync.transfers) and session.sync.transfers[-1].get("podgrove-transfer/payload/source")
               == b"source still synchronizes after port recreation")
    assert session.thread.is_alive()
    session.sync.close.assert_not_called()
    session.api.close.assert_not_called()
    session.kube.destroy.assert_not_called()
