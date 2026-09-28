import copy
from pathlib import Path

import pytest
import yaml

from podgrove.config import default_tainted_nodes, load_config, load_target
from podgrove.errors import PodgroveError
from podgrove.kube import engine_pod_manifest, manifests
from podgrove.placement import placement_spec, validate_placement


def render(placement=None, mode="shared", tainted=None):
    docs = manifests("team-dev", "123456abcdef", Path("/worktree/project"), "small", 600,
                     placement=placement, node_mode=mode, tainted_nodes=tainted)
    return engine_pod_manifest(next(item for item in docs if item["kind"] == "StatefulSet"))["spec"]


def required(*terms):
    return {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
        "nodeSelectorTerms": [{"matchExpressions": term} for term in terms],
    }}}}


def test_omitted_placement_preserves_shared_defaults_without_selecting_a_provider_pool():
    spec = render()
    assert spec["nodeSelector"] == {"kubernetes.io/os": "linux"}
    assert spec["tolerations"] == []
    assert spec["affinity"] == {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
        "nodeSelectorTerms": [{"matchExpressions": [
            {"key": "eks.amazonaws.com/compute-type", "operator": "NotIn", "values": ["fargate", "auto"]},
        ]}],
    }}}


def test_explicit_on_demand_pool_keeps_linux_and_dedicated_constraints():
    requested = {"nodeSelector": {"example.com/capacity-type": "on-demand"},
                 "tolerations": [{"key": "pool", "operator": "Equal", "value": "builders", "effect": "NoSchedule"}]}
    before = copy.deepcopy(requested)
    spec = render(requested, mode="tainted")
    assert spec["nodeSelector"] == {"kubernetes.io/os": "linux", "podgrove.dev/dedicated": "true",
                                    "example.com/capacity-type": "on-demand"}
    assert spec["tolerations"] == [{"operator": "Equal", "key": "dedicated", "value": "podgrove", "effect": "NoSchedule"},
                                  requested["tolerations"][0]]
    assert requested == before


def test_each_or_term_retains_mandatory_compute_constraint_and_preferences_are_kept():
    requested = required([{"key": "pool", "operator": "In", "values": ["one"]}],
                         [{"key": "pool", "operator": "In", "values": ["two"]}])
    preferred = [{"weight": 80, "preference": {"matchExpressions": [{"key": "zone", "operator": "In", "values": ["near"]}]}}]
    requested["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"] = preferred
    before = copy.deepcopy(requested)
    affinity = render(requested)["affinity"]["nodeAffinity"]
    terms = affinity["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert [term["matchExpressions"][0]["values"] for term in terms] == [["one"], ["two"]]
    assert all(term["matchExpressions"][-1] == {
        "key": "eks.amazonaws.com/compute-type", "operator": "NotIn", "values": ["fargate", "auto"],
    } for term in terms)
    assert affinity["preferredDuringSchedulingIgnoredDuringExecution"] == preferred
    assert requested == before


@pytest.mark.parametrize("placement", [
    {"nodeSelector": {"kubernetes.io/os": "windows"}},
    {"nodeSelector": {"eks.amazonaws.com/compute-type": "fargate"}},
    {"nodeSelector": {"eks.amazonaws.com/compute-type": "auto"}},
    required([{"key": "kubernetes.io/os", "operator": "NotIn", "values": ["linux"]}]),
    required([{"key": "kubernetes.io/os", "operator": "DoesNotExist"}]),
    required([{"key": "eks.amazonaws.com/compute-type", "operator": "In", "values": ["auto", "fargate"]}]),
])
def test_conflicts_with_required_engine_placement_are_refused(placement):
    with pytest.raises(PodgroveError, match="placement"):
        render(placement)


def test_conflicting_dedicated_selector_is_rejected_but_shared_selector_is_independent():
    setting = {"nodeSelector": {"podgrove.dev/dedicated": "false"}}
    assert render(setting)["nodeSelector"]["podgrove.dev/dedicated"] == "false"
    with pytest.raises(PodgroveError, match="conflicts"):
        render(setting, mode="tainted")


@pytest.mark.parametrize("placement", [
    [], {"nodeName": "no"}, {"nodeSelector": {"invalid key": "x"}}, {"nodeSelector": {"pool": True}},
    {"nodeSelector": {"pool": "x" * 64}}, {"affinity": {"podAffinity": {}}},
    {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": []}}}},
    {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{}]}}}},
    required([]), required([{"key": "pool", "operator": "In", "values": []}]),
    required([{"key": "pool", "operator": "Exists", "values": ["no"]}]),
    required([{"key": "pool", "operator": "Gt", "values": ["no"]}]),
    required([{"key": "pool", "operator": "Lt", "values": ["1", "2"]}]),
    required([{"key": "pool", "operator": "Gt", "values": [str(2**63)]}]),
    {"tolerations": [{}]}, {"tolerations": [{"operator": "Exists", "value": "no"}]},
    {"tolerations": [{"key": "pool", "tolerationSeconds": 5}]},
    {"tolerations": [{"key": "pool", "effect": "NoExecute", "tolerationSeconds": True}]},
    {"tolerations": [{"key": "pool"}, {"key": "pool", "operator": "Equal"}]},
    {"tolerations": [{"key": "invalid key", "operator": "Exists"}]},
])
def test_invalid_or_unsupported_native_placement_is_rejected_before_render(placement):
    with pytest.raises(PodgroveError, match="placement"):
        validate_placement(placement)


def test_valid_noexecute_and_exists_tolerations_are_explicit_and_retained():
    tolerations = [{"key": "pool", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 0},
                   {"operator": "Exists"}]
    assert render({"tolerations": tolerations})["tolerations"] == tolerations


def test_config_semantics_are_applied_to_target_only_reads_too(tmp_path):
    (tmp_path / "podgrove.yml").write_text(yaml.safe_dump({"placement": {"nodeSelector": {"kubernetes.io/os": "windows"}}}))
    with pytest.raises(PodgroveError, match="placement"):
        load_target(tmp_path)


def test_config_retains_native_placement_without_changing_the_input(tmp_path):
    setting = {"nodeSelector": {"example.com/capacity-type": "on-demand"}}
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / "podgrove.yml").write_text(yaml.safe_dump({"placement": setting}))
    config = load_config(tmp_path)
    assert config.placement == setting
    assert placement_spec(config.placement, node_mode=config.node_mode, tainted_nodes=default_tainted_nodes())["nodeSelector"]["kubernetes.io/os"] == "linux"


def test_numeric_node_affinity_retains_signed_integer_semantics():
    setting = required([{"key": "capacity", "operator": "Gt", "values": ["-1"]}])
    assert render(setting)["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"] == ["-1"]
    with pytest.raises(PodgroveError, match="label values"):
        render(required([{"key": "capacity", "operator": "In", "values": ["-1"]}]))
