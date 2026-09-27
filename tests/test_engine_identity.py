"""Engine replacement evidence survives mid-build failures without replay."""
from copy import deepcopy
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from podgrove import runtime, state
from podgrove.config import Config
from podgrove.docker_tunnel import DockerTunnel, EngineReplacedError, _VerificationUnavailable
from podgrove.errors import PodgroveError
from podgrove.kube import ENVIRONMENT
from test_docker_tunnel import FakeKube, IDENT


@pytest.mark.parametrize("replaced", ["pod", "statefulset"])
def test_fresh_probe_retains_captured_uids_and_reports_replacement_with_zero_restarts(replaced):
    kube = FakeKube("raise SystemExit('no Docker requests are expected')")
    tunnel = DockerTunnel(kube, IDENT, 0, verification_interval=3600).start()
    try:
        original = tunnel.identity_snapshot()
        assert original["state"] == "verified" and original["expected"] == original["observed"]
        resource = kube.pod if replaced == "pod" else kube.controller
        resource["metadata"]["uid"] = "replacement-uid"
        kube.pod["status"] = {"containerStatuses": [{"name": "docker", "restartCount": 0}]}
        # Controller replacement deliberately leaves the old Pod owner ref in
        # place: the new controller UID must still be reported precisely.
        with pytest.raises(EngineReplacedError, match="not replayed.*up --refresh") as failure:
            tunnel.refresh_identity()
        assert "replacement-uid" in str(failure.value)
        assert ("pod-uid" if replaced == "pod" else "controller-uid") in str(failure.value)
        current = tunnel.identity_snapshot()
        assert current["state"] == "replaced"
        assert current["expected"] == original["expected"]
        assert current["observed"][replaced + "_uid"] == "replacement-uid"
        assert tunnel._uids == ("controller-uid", "pod-uid")
        assert len(kube.reads) == 4 and kube.commands == []
        with pytest.raises(PodgroveError, match="replaced"):
            tunnel.check()
        # Returned evidence is a copy, not a mutable authority for later execs.
        current["expected"]["pod_uid"] = "tampered"
        assert tunnel.identity_snapshot()["expected"]["pod_uid"] == "pod-uid"
    finally:
        tunnel.close()


def test_fresh_probe_outage_reports_unknown_current_identity_not_a_replacement(monkeypatch):
    kube = FakeKube("raise SystemExit('no Docker requests are expected')")
    tunnel = DockerTunnel(kube, IDENT, 0, verification_interval=3600).start()
    try:
        before = tunnel.identity_snapshot()
        monkeypatch.setattr(kube, "call", Mock(side_effect=PodgroveError("transport read timed out")))
        with pytest.raises(_VerificationUnavailable):
            tunnel.refresh_identity()
        current = tunnel.identity_snapshot()
        assert current["state"] == "unavailable" and current["observed"] is None
        assert current["expected"] == before["expected"]
        assert current["checked_at"] >= before["checked_at"]
        assert tunnel.snapshot()["verification"]["state"] == "unavailable"
        tunnel.check()  # A failed read alone is not confirmed replacement.
        assert kube.commands == []
    finally:
        tunnel.close()


def test_confirmed_replacement_evidence_survives_a_later_diagnostic_read_outage(monkeypatch):
    kube = FakeKube("raise SystemExit('no Docker requests are expected')")
    tunnel = DockerTunnel(kube, IDENT, 0, verification_interval=3600).start()
    try:
        kube.pod["metadata"]["uid"] = "replacement-pod-uid"
        with pytest.raises(EngineReplacedError):
            tunnel.refresh_identity()
        confirmed = tunnel.identity_snapshot()
        monkeypatch.setattr(kube, "call", Mock(side_effect=PodgroveError("network unavailable")))
        with pytest.raises(_VerificationUnavailable):
            tunnel.refresh_identity()
        assert tunnel.identity_snapshot() == confirmed
        assert confirmed["state"] == "replaced" and confirmed["observed"]["pod_uid"] == "replacement-pod-uid"
    finally:
        tunnel.close()


def test_confirmed_replacement_is_latched_even_if_a_later_read_returns_original_uids():
    kube = FakeKube("raise SystemExit('no Docker requests are expected')")
    tunnel = DockerTunnel(kube, IDENT, 0, verification_interval=3600).start()
    try:
        kube.pod["metadata"]["uid"] = "replacement-pod-uid"
        with pytest.raises(EngineReplacedError):
            tunnel.refresh_identity()
        confirmed = tunnel.identity_snapshot()
        kube.pod["metadata"]["uid"] = "pod-uid"
        assert tunnel.refresh_identity() == confirmed
        with pytest.raises(PodgroveError, match="replaced"):
            tunnel.check()
        assert kube.commands == []
    finally:
        tunnel.close()


@pytest.mark.parametrize("change", ["pod", "controller", "unavailable", "unchanged"])
def test_supervisor_persists_original_and_failure_time_identity_before_any_build_replay(tmp_path, monkeypatch,
                                                                                    capsys, change):
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    ident = state.identity(tmp_path)
    token = uuid.uuid4().hex
    socket_path = Path(tempfile.gettempdir()) / f"podgrove-{os.getuid()}-{token[:16]}.sock"
    path = state.state_path(tmp_path, "offline-identity")
    record = {"identity": ident, "root": str(tmp_path), "context": "offline-identity", "namespace": "default",
              "namespace_mode": "shared", "timeout": 5, "status": "starting", "token": token,
              "socket": str(socket_path), "ttl_seconds": 3600}
    state.write(path, record)
    kube = FakeKube("raise SystemExit('no Docker requests are expected')")
    for resource in (kube.controller, kube.pod):
        metadata = resource["metadata"]
        metadata["name"] = metadata["name"].replace(IDENT, ident)
        metadata["labels"][ENVIRONMENT] = ident
    kube.pod["metadata"]["ownerReferences"][0]["name"] = "pg-" + ident
    kube.pod["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] = "pg-" + ident
    kube.wait, kube.heartbeat, kube.destroy = Mock(), Mock(), Mock()
    monkeypatch.setattr(runtime, "Kube", lambda *_args, **_kwargs: kube)
    monkeypatch.setattr(runtime.signal, "signal", lambda *_: None)
    config = Config(tmp_path, [])
    compose = Mock(config=config)
    compose.model.return_value = {"services": {"app": {"image": "offline-fixture"}}}
    monkeypatch.setattr(runtime, "load_config", lambda *_: config)
    monkeypatch.setattr(runtime, "Compose", lambda *_: compose)
    run = Mock(return_value=SimpleNamespace(stdout="", stderr=""))
    monkeypatch.setattr(runtime, "run", run)
    tunnels = []
    def make_tunnel(*args):
        tunnel = DockerTunnel(*args, verification_interval=3600)
        tunnels.append(tunnel)
        return tunnel
    monkeypatch.setattr(runtime, "DockerTunnel", make_tunnel)
    starting = []
    def fail_build(*_):
        observed = state.read(path)
        assert observed["status"] == "starting"
        assert observed["engine_identity"]["expected"] == {"statefulset_uid": "controller-uid", "pod_uid": "pod-uid"}
        starting.append(deepcopy(observed["engine_identity"]))
        if change == "pod":
            kube.pod["metadata"]["uid"] = "fresh-pod-uid"
            kube.pod["status"] = {"containerStatuses": [{"restartCount": 0}]}
        elif change == "controller":
            kube.controller["metadata"]["uid"] = "fresh-controller-uid"
        elif change == "unavailable":
            monkeypatch.setattr(kube, "call", Mock(side_effect=PodgroveError("network unavailable")))
        raise PodgroveError("build stream ended with unexpected EOF")
    launch = Mock(side_effect=fail_build)
    monkeypatch.setattr(runtime, "launch_stack", launch)
    try:
        assert runtime.serve(path) == 1
    finally:
        socket_path.unlink(missing_ok=True)
    result = state.read(path)
    assert result["status"] == "error" and result["error"].startswith("build stream ended with unexpected EOF")
    assert result["engine_identity"]["expected"] == starting[0]["expected"]
    assert result["engine_identity"]["checked_at"] >= starting[0]["checked_at"]
    if change in ("pod", "controller"):
        assert result["engine_identity"]["state"] == "replaced"
        assert "fresh-" in result["error"] and "not replayed" in result["error"]
        assert "up --refresh" in capsys.readouterr().err
    elif change == "unavailable":
        assert result["engine_identity"]["state"] == "unavailable"
        assert result["engine_identity"]["observed"] is None
        assert "verification is unavailable" in result["error"] and "replaced" not in result["error"]
    else:
        assert result["engine_identity"]["state"] == "verified"
        assert result["engine_identity"]["expected"] == result["engine_identity"]["observed"]
        assert result["error"] == "build stream ended with unexpected EOF"
    launch.assert_called_once()
    run.assert_called_once()
    assert run.call_args.args[0] == ["docker", "info"]
    assert kube.commands == []  # Only identity reads; no mutation was retried.
    kube.destroy.assert_not_called()
    assert tunnels[0]._stopped.is_set() and not tunnels[0]._verification_thread.is_alive()
    assert not socket_path.exists()
