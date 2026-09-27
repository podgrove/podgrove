"""Independent supervisor failure-path tests with real private control sockets."""
from pathlib import Path

import pytest

from podgrove import runtime, state
from podgrove.errors import PodgroveError
from test_runtime import supervised_session as supervised_session


def test_worker_cleanup_preserves_primary_and_does_not_remove_helper(supervised_session, monkeypatch, capsys):
    session = supervised_session
    close = runtime.SyncWorker.close

    def close_then_fail(worker):
        close(worker)  # Stop the real local test worker before simulating failed confirmation.
        raise PodgroveError("worker shutdown was not confirmed")

    monkeypatch.setattr(runtime.SyncWorker, "close", close_then_fail)
    session.fail_tunnel.set()
    session.thread.join(timeout=5)
    assert session.results == [1]
    final = state.read(session.path)
    assert final["status"] == "error"
    assert final["error"].startswith("Kubernetes port-forward disconnected;")
    assert "Sync worker cleanup: worker shutdown was not confirmed" in final["error"]
    assert "worker shutdown was not confirmed" in capsys.readouterr().err
    session.sync.close.assert_not_called()
    assert "tunnel-close" in session.events
    assert not Path(session.data["socket"]).exists()
    session.kube.destroy.assert_not_called()


@pytest.mark.parametrize("cleanup", ["helper", "tunnel"])
def test_cleanup_failure_preserves_primary_and_finishes_local_teardown(supervised_session, monkeypatch, cleanup):
    session = supervised_session

    def failed_close(*_args):
        raise PodgroveError(f"{cleanup} removal failed")

    if cleanup == "helper":
        session.sync.close.side_effect = failed_close
    else:
        monkeypatch.setattr(runtime.DockerTunnel, "close", failed_close)
    session.fail_watch.set()
    session.thread.join(timeout=5)
    assert session.results == [1]
    final = state.read(session.path)
    assert final["status"] == "error"
    assert final["error"].startswith("docker compose watch exited;")
    assert f"{cleanup} removal failed" in final["error"]
    assert not Path(session.data["socket"]).exists()
    session.sync.close.assert_called_once()
    session.kube.destroy.assert_not_called()


def test_clean_stop_reports_cleanup_failure_in_retry_state(supervised_session, monkeypatch):
    session = supervised_session

    def failed_close(*_args):
        raise PodgroveError("tunnel child did not stop")

    monkeypatch.setattr(runtime.DockerTunnel, "close", failed_close)
    assert runtime.control(session.data, "stop")["ok"]
    session.thread.join(timeout=5)
    assert session.results == [1]
    final = state.read(session.path)
    assert final["status"] == "error"
    assert final["error"] == "Tunnel cleanup: tunnel child did not stop"
    assert not Path(session.data["socket"]).exists()
    session.kube.destroy.assert_not_called()


def test_one_failed_tunnel_close_does_not_skip_remaining_tunnel(request, monkeypatch):
    monkeypatch.setattr(runtime, "port_plan", lambda *_args, **_kwargs: [{"local": 12345, "published": 80}])
    session = request.getfixturevalue("supervised_session")
    assert session.events.count("tunnel-start") == 2
    closed = []

    def close_one_then_fail(tunnel):
        closed.append(tunnel)
        if len(closed) == 1:
            raise PodgroveError("first tunnel cleanup failed")

    monkeypatch.setattr(runtime.DockerTunnel, "close", close_one_then_fail)
    session.fail_watch.set()
    session.thread.join(timeout=5)
    assert session.results == [1]
    assert len(closed) == 2 and closed[0] is not closed[1]
    final = state.read(session.path)
    assert final["status"] == "error"
    assert final["error"].startswith("docker compose watch exited;")
    assert "first tunnel cleanup failed" in final["error"]
    assert not Path(session.data["socket"]).exists()
