import socket
from unittest.mock import Mock

import pytest

from podgrove.errors import PodgroveError
from podgrove.forward import Tunnel, port_plan
from podgrove.kube import Kube


def published(service="api", target=80, port=8080):
    return {"service": service, "target": target, "published": port, "protocol": "tcp"}


def test_auto_forward_skips_occupied_worktree_preference():
    ident = "123456abcdef"
    preferred = 20000 + (8080 + int(ident[:8], 16) % 20000) % 40000
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", preferred))
        listener.listen()
        result = port_plan([published()], None, ident)
        assert result[0]["local"] != preferred
        assert result[0]["url"].startswith("http://127.0.0.1:")


def test_explicit_forward_never_reuses_occupied_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        local = listener.getsockname()[1]
        with pytest.raises(PodgroveError, match="already in use"):
            port_plan([published()], [{"service": "api", "port": 80, "local": local}], "123456abcdef")


def test_multiple_services_unique_local_ports_and_disabled_forwarding():
    assert port_plan([published()], [], "123456abcdef") == []
    ports = port_plan([published(), published("worker", 9000, 48080)], None, "123456abcdef")
    assert len({p["local"] for p in ports}) == 2


def test_missing_explicit_target_is_actionable():
    with pytest.raises(PodgroveError, match="exactly one"):
        port_plan([published()], [{"service": "missing", "port": 80}], "123456abcdef")


def test_tunnel_targets_stable_engine_pod_and_only_localhost(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    commands = []

    def start(command, *, stdout, stderr, stdin):
        commands.append(command)
        assert stderr is stdout
        stdout.write(b"Forwarding from 127.0.0.1:30275 -> 2375\n")
        stdout.flush()
        return process

    monkeypatch.setattr("podgrove.forward.subprocess.Popen", start)
    monkeypatch.setattr(Tunnel, "_verify_engine", lambda self: None)
    monkeypatch.setattr(Tunnel, "_listeners_ready", lambda self: True)
    process.wait.side_effect = lambda **_: setattr(process.poll, "return_value", -15)
    tunnel = Tunnel(Kube("chosen-cluster", "default"), "123456abcdef", [(30275, 2375)])
    try:
        tunnel.start(timeout=1)
        command = commands[0]
        assert command[command.index("--context") + 1] == "chosen-cluster"
        assert command[command.index("--namespace") + 1] == "default"
        assert command[command.index("port-forward") + 1] == "pod/pg-123456abcdef-0"
        assert "--address=127.0.0.1" in command
        assert command[-1] == "30275:2375"
    finally:
        tunnel.close()
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=1)
