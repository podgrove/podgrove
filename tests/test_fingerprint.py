"""Legacy hash upgrades must not restart an unchanged running application."""
from copy import deepcopy
import hashlib
import json
import shutil
from unittest.mock import Mock

import pytest

from podgrove import __version__, cli, runtime, state
from podgrove.compose import Compose
from podgrove.config import Config, load_config
from podgrove.fingerprint import FORMAT, launch_fingerprint


def legacy_hash(model, config):
    # The exact payload used by the release before network configuration was
    # added; this independent implementation represents persisted old state.
    payload = {"model": model, "forward": config.forward, "ttl": config.ttl_seconds}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def test_current_serialization_stays_compatible_and_does_not_mutate_input(tmp_path):
    config = Config(tmp_path, [])
    model = {"services": {"app": {"image": "alpine:3.21", "command": ["echo", "one", "two"]}}}
    before = deepcopy(model)
    payload = {"model": model, "forward": None, "ttl": 28800, "network": {"blocked_cidrs": []}}
    fingerprint = launch_fingerprint(model, config)
    assert fingerprint.digest == hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    assert fingerprint.matches(fingerprint.digest)
    assert fingerprint.matches(fingerprint.digest, FORMAT)
    assert model == before
    model["services"]["app"]["command"] = ["echo", "two", "one"]
    assert launch_fingerprint(model, config).digest != fingerprint.digest


@pytest.mark.parametrize("network,excluded", [({"blocked_cidrs": ["203.0.113.0/24"]}, []),
                                            ({"blocked_cidrs": []}, ["__pycache__"])])
def test_legacy_match_requires_default_new_settings(tmp_path, network, excluded):
    config = Config(tmp_path, [], network=network, sync_exclude=excluded)
    model = {"services": {"app": {"image": "alpine:3.21"}}}
    fingerprint = launch_fingerprint(model, config)
    assert fingerprint.legacy_digest is None
    assert not fingerprint.matches(legacy_hash(model, config))


@pytest.mark.parametrize("recorded_format", [FORMAT, "future-format", 1, False])
def test_a_tagged_record_never_uses_pre_network_compatibility(tmp_path, recorded_format):
    config = Config(tmp_path, [])
    model = {"services": {"app": {"image": "alpine:3.21"}}}
    fingerprint = launch_fingerprint(model, config)
    assert fingerprint.matches(legacy_hash(model, config))
    assert not fingerprint.matches(legacy_hash(model, config), recorded_format)
    if recorded_format != FORMAT:
        assert not fingerprint.matches(fingerprint.digest, recorded_format)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "worktree"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("PODGROVE_FINGERPRINT_TEST_MESSAGE", raising=False)
    (root / "fingerprint.env").write_text("PODGROVE_FINGERPRINT_TEST_MESSAGE=first\n")
    (root / "compose.yml").write_text(
        'name: fingerprint-regression\nservices:\n  api:\n    image: alpine:3.21\n'
        '    command: ["echo", "${PODGROVE_FINGERPRINT_TEST_MESSAGE:?required}"]\n'
        '    ports: ["18080:8080"]\n'
    )
    (root / "compose.ci.yml").write_text('services:\n  api:\n    environment: {LANE: ci}\n')
    (root / "compose.integration.yml").write_text('services:\n  api:\n    environment: {TESTS: integration}\n')
    (root / "podgrove.yml").write_text(
        'cluster: {context: fingerprint-test, namespace: approved}\ncompose:\n'
        '  files: [compose.yml, compose.ci.yml, compose.integration.yml]\n'
        '  env_file: fingerprint.env\n'
    )
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    started = []
    def spawn(path):
        data = state.read(path)
        started.append(path)
        data.update(status="ready", pid=100 + len(started), ports=[],
                    forward_status={"state": "ready"}, docker_host="tcp://127.0.0.1:12345")
        state.write(path, data)
    monkeypatch.setattr(runtime, "spawn", spawn)
    monkeypatch.setattr(runtime, "is_running", lambda _: bool(started))
    control = Mock(return_value={"ok": True, "forward_status": {"state": "ready"}})
    monkeypatch.setattr(runtime, "control", control)
    return root, kube, started, control


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose CLI required; daemon not used")
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("change", [None, "model", "forward", "ttl", "network", "sync"])
def test_real_compose_cold_first_second_up_and_meaningful_changes(project, capsys, legacy, change):
    root, kube, started, control = project
    path = state.state_path(root, "fingerprint-test")
    def up():
        assert cli.execute(cli.parser().parse_args(["up", "--json"])) == 0
        return json.loads(capsys.readouterr().out)
    cold = up()
    assert len(started) == 1
    assert cold["compose_fingerprint_format"] == FORMAT
    assert cold["podgrove_version"] == __version__
    config = load_config(root)
    original_model = Compose(config).model()
    if legacy:
        legacy_record = state.read(path)
        legacy_record.pop("compose_fingerprint_format")
        legacy_record.pop("podgrove_version")
        legacy_record["compose_fingerprint"] = legacy_hash(original_model, config)
        state.write(path, legacy_record)
    original_record = state.read(path)
    assert up()["pid"] == cold["pid"]
    assert up()["pid"] == cold["pid"]
    assert Compose(load_config(root)).model() == original_model
    assert state.read(path) == original_record  # Never race the running supervisor by rewriting its state.
    assert len(started) == kube.create_environment.call_count == 1
    assert [call.args[1] for call in control.call_args_list] == ["ping", "ping"]
    if change is None:
        return
    if change == "model":
        (root / "fingerprint.env").write_text("PODGROVE_FINGERPRINT_TEST_MESSAGE=second\n")
    else:
        additions = {"forward": "forward: []\n", "ttl": "ttl: 9h\n",
                     "network": "network: {blocked_cidrs: ['203.0.113.0/24']}\n",
                     "sync": "sync: {exclude: [__pycache__]}\n"}
        config_file = root / "podgrove.yml"
        config_file.write_text(config_file.read_text() + additions[change])
    changed = up()
    assert len(started) == kube.create_environment.call_count == 2
    assert changed["pid"] != cold["pid"]
    assert changed["compose_fingerprint_format"] == FORMAT
    assert changed["compose_fingerprint"] != cold["compose_fingerprint"]
    assert [call.args[1] for call in control.call_args_list].count("stop") == 1
    kube.destroy.assert_not_called()
