from types import SimpleNamespace

import pytest

from scripts.e2e import Harness


def test_restart_rediscovers_docker_allocated_api_and_application_ports(tmp_path, monkeypatch):
    harness = Harness(tmp_path, mongo=False)
    observed = []
    ports = {"2375/tcp": 41001, "8080/tcp": 41002, "8081/tcp": 41003}

    def docker(args):
        observed.append(args)
        assert args[:3] == ["docker", "port", "podgrove-owned"]
        return SimpleNamespace(stdout=f"127.0.0.1:{ports[args[3]]}\n")

    monkeypatch.setattr(harness, "run", docker)
    stale_env = {"DOCKER_HOST": "tcp://127.0.0.1:32001", "PATH": "/bin"}
    updated, app_port, watch_port = harness.endpoints("podgrove-owned", stale_env)
    assert updated == {"DOCKER_HOST": "tcp://127.0.0.1:41001", "PATH": "/bin"}
    assert (app_port, watch_port) == (41002, 41003)
    assert stale_env["DOCKER_HOST"] == "tcp://127.0.0.1:32001"
    assert len(observed) == 3


def test_low_disk_refuses_before_any_docker_command(tmp_path, monkeypatch):
    harness = Harness(tmp_path, mongo=False)
    monkeypatch.setattr("scripts.e2e.shutil.disk_usage", lambda _: SimpleNamespace(free=1024 ** 3))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Docker must not be called when disk headroom is insufficient")

    monkeypatch.setattr(harness, "run", forbidden)
    with pytest.raises(RuntimeError, match="No test engine was created"):
        harness.require_local_engine()
