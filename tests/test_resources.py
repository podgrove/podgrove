"""Custom worktree budgets reach every read/launch path without hidden caps."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from podgrove import cli, runtime, state, web
from podgrove.compose import Compose
from podgrove.config import CONFIG_SCHEMA, load_cluster, load_config
from podgrove.errors import PodgroveError
from podgrove.kube import Kube, manifests
from podgrove.resources import engine_resources, initializer_resources, quantity, resource_budget, same_resources

CUSTOM = {"requests": {"cpu": "1500m", "memory": "6Gi", "ephemeral-storage": "1Gi"},
          "limits": {"cpu": "6", "memory": "12Gi", "ephemeral-storage": "8Gi"}}
INIT = {"requests": {"cpu": "20m", "memory": "32Mi"}, "limits": {"cpu": "200m", "memory": "64Mi"}}
IDENT = "aabbccddeeff"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "compose.yml").write_text("services: {app: {image: alpine:3.21}}\n")
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(Compose, "model", lambda _: {"name": "fixture", "services": {"app": {"image": "alpine:3.21"}}})
    return root


def configure(root, **values):
    (root / "podgrove.yml").write_text(yaml.safe_dump({
        "cluster": {"context": "fixture-context", "namespace": "fixture-namespace", "storage_class": "fixture-class"},
        **values,
    }))


def args(root, command="up", *flags):
    return cli.parser().parse_args([command, "--project-directory", str(root), *flags])


def pod(rendered):
    return next(row for row in rendered if row["kind"] == "StatefulSet")["spec"]["template"]["spec"]


@pytest.mark.parametrize(("value", "expected"), [
    ("1500m", "1.5"), ("1.5", "1.5"), (1.5, "1.5"), (2, "2"),
    ("6Gi", "6442450944"), ("1536Mi", "1610612736"), ("1.5G", "1500000000"),
    ("1e3", "1000"), ("1E+3", "1000"), ("1e-3", ".001"), ("+2", "2"),
    (".5", ".5"), ("1000u", ".001"), ("1n", ".000000001"),
    ("1Pi", "1125899906842624"), ("1000000", "1000000"),
])
def test_kubernetes_decimal_binary_and_exponent_quantities(value, expected):
    assert quantity(value) == Decimal(expected)


@pytest.mark.parametrize("value", [
    True, False, None, [], {}, "", "0", 0, -1, "-1Gi", "1 GB", "2gi", "1K", "5GB", "NaN", ".inf",
    float("nan"), float("inf"), float("-inf"), "1e999999999999999999", "1e-999999999999999999", "1e1000",
    "1e-1000", "1e-10", "10Ei", "1" * 129, "1Gi\n", " 1Gi", "1Gi ",
])
def test_invalid_nonfinite_and_unrepresentable_quantities_are_local_errors(value):
    with pytest.raises(PodgroveError, match="quantity"):
        quantity(value, "storage.size")


@pytest.mark.parametrize("value", [".0001", "0.5m", "100u", "1n", "1001u"])
def test_cpu_must_be_whole_millicores(value):
    with pytest.raises(PodgroveError, match="millicore"):
        resource_budget({"requests": {"cpu": value}})


@pytest.mark.parametrize("value", [".001", "1m", "1000u", "1000000n", "1e-3", "1.0000"])
def test_cpu_equivalent_millicore_forms(value):
    assert resource_budget({"requests": {"cpu": value}}) == {"requests": {"cpu": value}}


@pytest.mark.parametrize(("field", "requested", "limit"), [
    ("cpu", "1500m", "1"), ("memory", "1Gi", "1000M"), ("ephemeral-storage", "2G", "1Gi"),
])
def test_request_limit_comparison_uses_values_not_strings(field, requested, limit):
    with pytest.raises(PodgroveError, match=f"requests.{field}"):
        resource_budget({"requests": {field: requested}, "limits": {field: limit}})


def test_exact_replacement_and_defaults_do_not_leak_or_mutate():
    assert engine_resources("large", {"requests": {"memory": "6Gi"}}) == {"requests": {"memory": "6Gi"}}
    assert engine_resources("small", {}) == {}
    assert engine_resources("small", {"limits": {"cpu": 3}, "requests": {}}) == {"limits": {"cpu": "3"}}
    assert initializer_resources({}) == {}
    presets = engine_resources("small")
    presets["limits"]["memory"] = "99Gi"
    assert engine_resources("small")["limits"]["memory"] == "2Gi"
    initial = initializer_resources()
    initial["limits"].clear()
    assert initializer_resources()["limits"] == {"cpu": "100m", "memory": "32Mi"}


@pytest.mark.parametrize("zero", [0, "0", "0m", "0.0", "0Gi"])
def test_explicit_zero_requests_remain_distinct_from_defaulting(zero):
    budget = {"requests": {"cpu": zero, "memory": zero, "ephemeral-storage": zero},
              "limits": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "2Gi"}}
    parsed = resource_budget(budget)
    assert set(parsed["requests"]) == {"cpu", "memory", "ephemeral-storage"}
    assert same_resources(budget, parsed)
    assert not same_resources({"limits": budget["limits"]}, parsed)
    assert initializer_resources(budget) == parsed


def test_zero_limits_validate_against_requests_and_pvc_remains_positive(project):
    assert resource_budget({"limits": {"cpu": 0, "memory": "0"}}) == {"limits": {"cpu": "0", "memory": "0"}}
    with pytest.raises(PodgroveError, match="request must not exceed"):
        resource_budget({"requests": {"cpu": "1"}, "limits": {"cpu": 0}})
    configure(project, resources={"requests": {"cpu": 0}, "limits": {"cpu": "1"}},
              init_resources={"limits": {"memory": 0}})
    assert load_config(project).resources["requests"]["cpu"] == "0"
    configure(project, storage={"size": 0})
    with pytest.raises(PodgroveError, match="storage.size"):
        load_config(project)


@pytest.mark.parametrize("body", [
    "resources: null", "resources: []", "resources: {request: {cpu: 1}}",
    "resources: {requests: {gpu: 1}}", "resources: {requests: {cpu: true}}",
    "resources: {limits: {memory: null}}", "resources: {limits: {memory: .nan}}",
    "resources: {limits: {memory: .inf}}", "resources: {requests: {cpu: 1m}, limits: {cpu: 0.5m}}",
    "resources: {requests: {memory: 2Gi}, limits: {memory: 1Gi}}",
    "init_resources: {requests: {cpu: false}}", "init_resources: {limits: {memory: -1}}",
    "storage: null", "storage: {size: true}", "storage: {size: .inf}", "storage: {size: -2Gi}",
    "storage: {capacity: 20Gi}", "storage: {size: 1e999999}",
])
def test_config_and_target_loading_reject_invalid_budgets_before_any_compose_or_cluster(project, body):
    (project / "podgrove.yml").write_text(body)
    for load in (load_cluster, load_config):
        with pytest.raises(PodgroveError):
            load(project)


def test_normalized_config_and_published_schema(project):
    configure(project, size="small", resources=CUSTOM, init_resources=INIT, storage={"size": "40Gi"})
    config = load_config(project)
    assert config.resources == CUSTOM and config.init_resources == INIT and config.storage_size == "40Gi"
    schema_path = Path(__file__).resolve().parents[1] / "schema" / "podgrove-v1.schema.json"
    assert json.loads(schema_path.read_text()) == CONFIG_SCHEMA


@pytest.mark.parametrize(("flags", "expected", "capacity"), [
    ([], CUSTOM, "40Gi"), (["--storage", "60Gi"], CUSTOM, "60Gi"),
    (["--size", "small"], engine_resources("small"), "40Gi"),
    (["--size", "large", "--storage", "80Gi"], engine_resources("large"), "80Gi"),
])
def test_dry_run_custom_resources_and_cli_precedence(project, monkeypatch, capsys, flags, expected, capacity):
    configure(project, size="small", resources=CUSTOM, init_resources=INIT, storage={"size": "40Gi"})
    kube = Mock(side_effect=AssertionError("Dry run must not contact Kubernetes"))
    monkeypatch.setattr(cli, "Kube", kube)
    assert cli.execute(args(project, "up", "--dry-run", *flags)) == 0
    rendered = json.loads(capsys.readouterr().out)["resources"]
    assert pod(rendered)["containers"][0]["resources"] == expected
    assert pod(rendered)["initContainers"][0]["resources"] == INIT
    pvc = next(row for row in rendered if row["kind"] == "PersistentVolumeClaim")["spec"]
    assert pvc["resources"]["requests"]["storage"] == capacity
    assert pvc["storageClassName"] == "fixture-class"
    kube.assert_not_called()


@pytest.mark.parametrize("namespace_mode", ["shared", "worktree"])
def test_worktrees_load_independent_allocations_and_storage_without_state_or_cluster(
    tmp_path, monkeypatch, capsys, namespace_mode,
):
    state_home = tmp_path / "must-not-create-state"
    monkeypatch.setenv("PODGROVE_STATE_HOME", str(state_home))
    monkeypatch.setattr(Compose, "model", lambda _: {
        "name": "fixture", "services": {"app": {"image": "alpine:3.21"}}})
    kube = Mock(side_effect=AssertionError("Dry run must not contact Kubernetes"))
    monkeypatch.setattr(cli, "Kube", kube)
    monkeypatch.setattr(state, "write", Mock(side_effect=AssertionError("Dry run must not save state")))
    allocations = [
        ({"requests": {"cpu": "100m", "memory": "512Mi"}, "limits": {"memory": "1Gi"}}, "5Gi"),
        ({"requests": {"cpu": "2", "memory": "16Gi", "ephemeral-storage": "2Gi"},
          "limits": {"cpu": "8", "memory": "24Gi", "ephemeral-storage": "10Gi"}}, "80Gi"),
    ]
    rendered = []
    for number, (budget, capacity) in enumerate(allocations):
        root = tmp_path / f"worktree-{number}"
        root.mkdir()
        (root / "compose.yml").write_text("services: {app: {image: alpine:3.21}}\n")
        configure(root, resources=budget, storage={"size": capacity})
        config_path = root / "podgrove.yml"
        config = yaml.safe_load(config_path.read_text())
        config["cluster"]["namespace_mode"] = namespace_mode
        config_path.write_text(yaml.safe_dump(config))
        before = config_path.read_bytes()
        assert cli.execute(args(root, "up", "--dry-run")) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["identity"] == state.identity(root)
        assert pod(result["resources"])["containers"][0]["resources"] == budget
        pvc = next(row for row in result["resources"] if row["kind"] == "PersistentVolumeClaim")
        assert pvc["spec"]["resources"]["requests"]["storage"] == capacity
        assert pvc["metadata"]["name"] == "pg-" + result["identity"]
        assert all(row["metadata"]["namespace"] == result["namespace"] for row in result["resources"])
        assert config_path.read_bytes() == before
        rendered.append(result)
    assert rendered[0]["identity"] != rendered[1]["identity"]
    if namespace_mode == "shared":
        assert rendered[0]["namespace"] == rendered[1]["namespace"] == "fixture-namespace"
    else:
        assert {item["namespace"] for item in rendered} == {
            "fixture-namespace-wt-" + item["identity"] for item in rendered}
    kube.assert_not_called()
    state.write.assert_not_called()
    assert not state_home.exists()


def test_doctor_passes_custom_budgets_and_pvc_settings_to_admission(project, monkeypatch):
    configure(project, resources=CUSTOM, init_resources={}, storage={"size": "40Gi"})
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    assert cli.execute(args(project, "doctor")) == 0
    rendered = kube.check_admission.call_args.args[0]
    assert pod(rendered)["containers"][0]["resources"] == CUSTOM
    assert pod(rendered)["initContainers"][0]["resources"] == {}
    assert next(row for row in rendered if row["kind"] == "PersistentVolumeClaim")["spec"]["resources"]["requests"]["storage"] == "40Gi"
    kube.create_environment.assert_not_called()


def test_up_records_effective_budget_and_reuses_yaml_capacity(project, monkeypatch):
    monkeypatch.setattr(cli, "capture_anchor", lambda kube, ident: {"identity": ident})
    configure(project, resources=CUSTOM, init_resources=INIT, storage={"size": "40Gi"})
    kube = Mock()
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    def ready(path):
        current = state.read(path)
        current["status"] = "ready"
        state.write(path, current)
    monkeypatch.setattr(runtime, "spawn", ready)
    assert cli.execute(args(project)) == 0
    saved = state.read(state.state_path(project, "fixture-context"))
    assert saved["resources"] == CUSTOM and saved["init_resources"] == INIT
    assert saved["storage"] == {"size": "40Gi", "storage_class": "fixture-class"}
    rendered = kube.create_environment.call_args.args[0]
    assert pod(rendered)["containers"][0]["resources"] == CUSTOM
    monkeypatch.setattr(runtime, "is_running", lambda _: True)
    stop = Mock()
    monkeypatch.setattr(runtime, "control", stop)
    assert cli.execute(args(project)) == 0
    checked = kube.check_engine_settings.call_args.args[0]
    assert pod(checked)["containers"][0]["resources"] == CUSTOM
    stop.assert_not_called()
    assert kube.create_environment.call_count == 1


def test_reconnecting_retained_pvc_records_its_reused_storage_class(project, monkeypatch):
    monkeypatch.setattr(cli, "capture_anchor", lambda kube, ident: {"identity": ident})
    configure(project, storage={"size": "40Gi"})
    config_path = project / "podgrove.yml"
    config = yaml.safe_load(config_path.read_text())
    config["cluster"].pop("storage_class")
    config_path.write_text(yaml.safe_dump(config))
    kube = Mock()
    def reuse_class(rendered):
        pvc = next(row for row in rendered if row["kind"] == "PersistentVolumeClaim")
        assert "storageClassName" not in pvc["spec"]
        pvc["spec"]["storageClassName"] = "retained-owned-class"
    kube.check_storage.side_effect = reuse_class
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    def ready(path):
        current = state.read(path)
        current["status"] = "ready"
        state.write(path, current)
    monkeypatch.setattr(runtime, "spawn", ready)
    assert cli.execute(args(project)) == 0
    saved = state.read(state.state_path(project, "fixture-context"))
    assert saved["storage"] == {"size": "40Gi", "storage_class": "retained-owned-class"}


@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize("change", ["resources", "init_resources", "storage", "remove_limits"])
def test_incompatible_running_allocation_is_refused_before_any_mutation(project, monkeypatch, refresh, change):
    configure(project, resources=CUSTOM, init_resources=INIT, storage={"size": "40Gi"})
    ident = state.identity(project)
    original = manifests("fixture-namespace", ident, project, "medium", 28800,
                         resources=CUSTOM, init_resources=INIT, storage="40Gi", storage_class="fixture-class")
    controller = next(row for row in original if row["kind"] == "StatefulSet")
    pvc = next(row for row in original if row["kind"] == "PersistentVolumeClaim")
    kube = Kube("fixture-context", "fixture-namespace")
    def get(kind, name=None, *, selector=None, ignore_missing=True):
        if kind == "networkpolicies":
            assert name is None and selector == "app.kubernetes.io/managed-by=podgrove,podgrove.dev/component=connection"
            assert ignore_missing is False
            return {"items": []}
        assert selector is None and name == "pg-" + ident and ignore_missing is True
        assert kind.lower() in ("statefulset", "persistentvolumeclaim")
        return deepcopy(controller if kind.lower() == "statefulset" else pvc)
    kube.get = Mock(side_effect=get)
    kube.reconcile_network_policy = Mock(side_effect=AssertionError("No network write before compatible sizing"))
    kube.preflight = Mock(side_effect=AssertionError("No preflight required to refuse a changed allocation"))
    monkeypatch.setattr(cli, "Kube", Mock(return_value=kube))
    monkeypatch.setattr(runtime, "is_running", lambda _: True)
    control = Mock(side_effect=AssertionError("Do not interrupt the current supervisor"))
    monkeypatch.setattr(runtime, "control", control)
    saved = {"identity": ident, "context": "fixture-context", "root": str(project),
             "namespace": "fixture-namespace", "node_mode": "shared", "status": "ready"}
    path = state.state_path(project, "fixture-context")
    state.write(path, saved)
    settings = {"resources": deepcopy(CUSTOM), "init_resources": deepcopy(INIT), "storage": {"size": "40Gi"}}
    if change == "resources":
        settings["resources"]["limits"]["memory"] = "24Gi"
    elif change == "init_resources":
        settings["init_resources"]["limits"]["memory"] = "128Mi"
    elif change == "storage":
        settings["storage"]["size"] = "60Gi"
    else:
        settings["resources"].pop("limits")
    configure(project, **settings)
    with pytest.raises(PodgroveError, match="different (engine|storage) settings"):
        cli.execute(args(project, "up", *(["--refresh"] if refresh else [])))
    assert state.read(path) == saved
    control.assert_not_called()
    kube.reconcile_network_policy.assert_not_called()


def test_existing_kubernetes_quantity_canonicalization_and_defaulted_requests_are_compatible():
    assert same_resources({"limits": {"cpu": "1500m", "memory": "1Gi"}},
                          {"requests": {"cpu": "1.5", "memory": "1024Mi"},
                           "limits": {"cpu": "1.5", "memory": "1024Mi"}})
    assert not same_resources({}, engine_resources("medium"))
    desired = manifests("fixture-namespace", IDENT, Path("/fixture/worktree"), "small", 600)
    controller = next(row for row in desired if row["kind"] == "StatefulSet")
    actual = deepcopy(controller)
    budget = actual["spec"]["template"]["spec"]["containers"][0]["resources"]
    budget["requests"].update(cpu="0.25", memory="2048Mi", **{"ephemeral-storage": "4096Mi"})
    budget["limits"].update(cpu="2000m", memory="2048Mi", **{"ephemeral-storage": "4096Mi"})
    Kube("fixture-context", "fixture-namespace")._validate_existing(controller, actual, IDENT)


def test_web_current_config_includes_effective_engine_initializer_and_storage(project):
    configure(project, resources=CUSTOM, init_resources={}, storage={"size": "40Gi"})
    result = web.configuration_metadata({"root": str(project)})
    assert result["status"] == "available"
    assert result["settings"]["resources_mode"] == "custom"
    assert result["settings"]["resources"] == CUSTOM
    assert result["settings"]["init_resources"] == {}
    assert result["settings"]["storage"] == {"size": "40Gi"}
    configure(project, size="large")
    settings = web.configuration_metadata({"root": str(project)})["settings"]
    assert settings["resources_mode"] == "preset" and settings["resources"] == engine_resources("large")
    assert settings["storage"] == {"size": "20Gi"}


def test_web_invalid_request_limit_pair_is_not_presented_as_valid_config(project):
    configure(project, resources={"requests": {"cpu": "2"}, "limits": {"cpu": "1"}})
    result = web.configuration_metadata({"root": str(project)})
    assert result["status"] == "unavailable" and result["settings"] is None
