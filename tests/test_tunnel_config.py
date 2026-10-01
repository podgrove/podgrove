import copy
import re
from types import SimpleNamespace

import pytest
import yaml

from podgrove.config import Config, load_config, load_target, normalize_connect, normalize_reverse, refuse_legacy_connect
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


def refused(value):
    with pytest.raises(PodgroveError, match="no longer supported") as error:
        refuse_legacy_connect(SimpleNamespace(connect=normalize_connect(value)))
    return str(error.value)


def test_legacy_connect_requires_mutual_consent_migration():
    assert re.search('network.pod_to_pod: selected.*network.connect.*network.expose', refused([connection()]))


@pytest.mark.parametrize("value", [
    [connection(name="UPPER")], [connection(port=2375)], [connection(environment="nope")], [{"bogus": 1}],
    [connection(name=f"db-{index}") for index in range(33)], {"name": "database"}, "database", 7,
])
def test_malformed_legacy_connect_still_shows_the_migration_message(value):
    refused(value)


def test_nonempty_legacy_connections_are_refused_even_when_multiple():
    refused([connection(), connection(service="other")])


def write_legacy_project(tmp_path, connect):
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / "podgrove.yml").write_text(yaml.safe_dump({"connect": connect}))


@pytest.mark.parametrize("connect", [[connection()], [{"bogus": 1}]])
def test_down_and_status_still_load_a_file_with_legacy_connect(tmp_path, connect):
    write_legacy_project(tmp_path, connect)
    assert load_config(tmp_path).connect == connect
    assert load_target(tmp_path)


@pytest.mark.parametrize("command", ["validate", "up"])
def test_validate_and_up_refuse_legacy_connect_before_any_cluster_or_compose_call(tmp_path, monkeypatch, command):
    from podgrove import cli
    write_legacy_project(tmp_path, [{"bogus": 1}])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "Compose", lambda *args, **kwargs: pytest.fail("Compose must not run"))
    args = cli.parser().parse_args([command, "--context", "test-context", "--namespace", "podgrove-testing"])
    with pytest.raises(PodgroveError, match="no longer supported"):
        cli.execute(args)


def test_loaded_config_preserves_reverse_and_empty_legacy_connect(tmp_path):
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / "podgrove.yml").write_text(yaml.safe_dump({"reverse": [{"local_port": 8080}], "connect": []}))
    config = load_config(tmp_path)
    assert config.reverse == [{"local_port": 8080, "remote_port": 8080, "local_host": "127.0.0.1"}]
    assert config.connect == []


def test_target_read_for_down_accepts_legacy_connect_without_compose_files(tmp_path):
    (tmp_path / 'podgrove.yml').write_text(yaml.safe_dump({'connect': [connection()]}))
    assert load_target(tmp_path)


@pytest.mark.parametrize("setting", [{"reverse": [{"local_port": 80}]}])
def test_target_only_reads_validate_route_semantics_without_compose(tmp_path, setting):
    (tmp_path / "podgrove.yml").write_text(yaml.safe_dump(setting))
    with pytest.raises(PodgroveError):
        load_target(tmp_path)
