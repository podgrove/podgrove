import copy

import pytest
import yaml

from podgrove.config import Config, load_config, load_target, normalize_connect, normalize_reverse
from podgrove.errors import PodgroveError


def test_tunnel_entries_are_optional_and_defaults_are_fresh(tmp_path):
    first, second = Config(tmp_path, []), Config(tmp_path, [])
    assert first.reverse == first.connect == []
    first.reverse.append({"local_port": 8080})
    first.connect.append({"name": "db"})
    assert second.reverse == second.connect == []


def test_reverse_normalizes_literal_loopback_and_same_default_port():
    value = [{"local_port": 8080}, {"local_port": 80, "remote_port": 8081, "local_host": "::1"}]
    before = copy.deepcopy(value)
    assert normalize_reverse(value) == [
        {"local_port": 8080, "remote_port": 8080, "local_host": "127.0.0.1"},
        {"local_port": 80, "remote_port": 8081, "local_host": "::1"},
    ]
    assert value == before


@pytest.mark.parametrize("value", [
    [{"local_port": 80}], [{"local_port": 8080, "remote_port": 80}],
    [{"local_port": 2375}], [{"local_port": 8080, "remote_port": 2376}],
    [{"local_port": 8080}, {"local_port": 80, "remote_port": 8080}],
    [{"local_port": 8080, "local_host": "localhost"}],
    [{"local_port": 8080, "local_host": "0.0.0.0"}],
    [{"local_port": 8080, "local_host": "169.254.169.254"}],
    [{"local_port": True}], [{"local_port": 65536}], [{"remote_port": 8080}],
    [{"local_port": 8080, "service": "app"}],
    [{"local_port": 8080 + i} for i in range(33)],
])
def test_reverse_refuses_unsafe_or_ambiguous_routes(value):
    with pytest.raises(PodgroveError, match="reverse"):
        normalize_reverse(value)


def connection(**kwargs):
    return {"name": "database", "environment": "abcdef123456", "service": "mongo-secondary_1", "port": 27017, **kwargs}


def test_connect_preserves_exact_target_without_accepting_namespace_override():
    value = [connection()]
    assert normalize_connect(value) == value
    assert normalize_connect(value)[0] is not value[0]


@pytest.mark.parametrize("changes", [
    {"name": "UPPER"}, {"name": "outside.example"}, {"name": "-bad"}, {"name": "x" * 64},
    {"name": "localhost"}, {"name": "host"}, {"name": "docker"}, {"name": "host-docker-internal"},
    {"environment": "ABCDEF123456"}, {"environment": "abcdef12345"}, {"namespace": "other"}, {"context": "other"},
    {"service": "unsafe/name"}, {"service": "-option"}, {"service": "x" * 129},
    {"port": 2375}, {"port": 2376}, {"port": True}, {"port": 65536},
])
def test_connect_rejects_invalid_or_cross_scope_configuration(changes):
    with pytest.raises(PodgroveError, match="connect"):
        normalize_connect([connection(**changes)])


def test_connect_aliases_are_unique_and_bounded():
    with pytest.raises(PodgroveError, match="duplicate"):
        normalize_connect([connection(), connection(service="other")])
    with pytest.raises(PodgroveError, match="connect"):
        normalize_connect([connection(name=f"db-{i}") for i in range(33)])


def test_loaded_config_provides_both_normalized_lists(tmp_path):
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / "podgrove.yml").write_text(yaml.safe_dump({"reverse": [{"local_port": 8080}], "connect": [connection()]}))
    config = load_config(tmp_path)
    assert config.reverse == [{"local_port": 8080, "remote_port": 8080, "local_host": "127.0.0.1"}]
    assert config.connect == [connection()]


@pytest.mark.parametrize("setting", [{"reverse": [{"local_port": 80}]}, {"connect": [connection(name="host")]}])
def test_target_only_reads_validate_route_semantics_without_compose(tmp_path, setting):
    (tmp_path / "podgrove.yml").write_text(yaml.safe_dump(setting))
    with pytest.raises(PodgroveError):
        load_target(tmp_path)
