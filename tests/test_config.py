import json
from pathlib import Path

import pytest

from podgrove import config as config_module
from podgrove.config import CONFIG_SCHEMA, Config, default_tainted_nodes, load_cluster, load_config, load_target
from podgrove.errors import PodgroveError


@pytest.fixture
def project(tmp_path):
    (tmp_path / "compose.yaml").write_text("services:\n  app:\n    image: busybox:1.37\n")
    return tmp_path


def test_all_fields_optional(project):
    config = load_config(project)
    assert config.root == project
    assert config.files == [project / "compose.yaml"]
    assert config.profiles == []
    assert config.forward is None
    assert config.size == "medium"
    assert config.ttl_seconds == 28800
    assert config.node_mode == "shared"
    assert config.tainted_nodes == default_tainted_nodes()
    assert config.context is None and config.namespace is None


def test_full_config_and_file_override(project):
    (project / "dev.env").write_text("MODE=test\n")
    (project / "overlay.yml").write_text("services: {}\n")
    (project / "podgrove.yml").write_text(
        "version: 1\ncompose:\n  files: [compose.yaml, overlay.yml]\n"
        "  profiles: [test]\n  env_file: dev.env\n"
        "forward:\n  - service: app\n    port: 8080\n    local: 23001\n"
        "size: large\nttl: 2d\n"
    )
    config = load_config(project)
    assert config.files == [project / "compose.yaml", project / "overlay.yml"]
    assert config.profiles == ["test"]
    assert config.env_file == project / "dev.env"
    assert config.forward == [{"service": "app", "port": 8080, "local": 23001}]
    assert config.ttl_seconds == 172800
    assert config.size == "large"
    assert load_config(project, files=["overlay.yml"]).files == [project / "overlay.yml"]


@pytest.mark.parametrize(("text", "error"), [
    ("verison: 1", "verison"),
    ("compose:\n  profile: [test]", "profile"),
    ("forward: [{service: app, port: 80, target: 80}]", "target"),
    ("version: 2", "version"),
    ("version: true", "version"),
    ("compose: {files: []}", "compose.files"),
    ("size: huge", "size"),
    ("node_mode: exclusive", "node_mode"),
    ("node_mode: true", "node_mode"),
    ("node_mode: null", "node_mode"),
    ("node_mode: dedicated", "node_mode"),
    ("tainted_nodes: {seletor: {pool: dev}}", "seletor"),
    ("tainted_nodes: {taint: {efect: NoSchedule}}", "efect"),
    ("tainted_nodes: {selector: {}}", "selector"),
    ("tainted_nodes: {selector: {pool: true}}", "selector"),
    ("tainted_nodes: {taint: {effect: PreferNoSchedule}}", "effect"),
    ("tainted_nodes: {taint: {key: ''}}", "key"),
    ("ttl: 0h", "ttl"),
    ("ttl: -1h", "ttl"),
    ("ttl: 8", "ttl"),
    ("forward: [{service: app, port: 65536}]", "port"),
    ("forward: [{service: app, port: false}]", "port"),
    ("forward: [{service: app}]", "port"),
    ("forward: [{service: app, port: 80}, {service: app, port: 80, local: 3000}]", "duplicate"),
    ("forward: [{service: app, port: 80, local: 3000}, {service: db, port: 80, local: 3000}]", "duplicate"),
    ("version: 1\nversion: 1", "duplicate"),
    ("123: nope", "keys must be strings"),
    ("- one\n- two", "not of type"),
    ("compose: [", "Cannot read"),
])
def test_invalid_configuration_is_actionable(project, text, error):
    (project / "podgrove.yml").write_text(text)
    with pytest.raises(PodgroveError, match=error):
        load_config(project)


def test_empty_configuration_and_empty_forward_list(project):
    (project / "podgrove.yml").write_text("")
    assert load_config(project).forward is None
    (project / "podgrove.yml").write_text("forward: []")
    assert load_config(project).forward == []


def test_missing_explicit_config_is_error(project):
    with pytest.raises(PodgroveError, match="path does not exist"):
        load_config(project, Path("not-here.yml"))


@pytest.mark.parametrize("relative", ["../outside.yml", ".git/compose.yml"])
def test_compose_file_cannot_escape_root_or_use_git(project, relative):
    with pytest.raises(PodgroveError, match="outside the worktree|.git paths"):
        load_config(project, files=[relative])


def test_symlink_escape_is_rejected(project):
    outside = project.parent / "outside.env"
    outside.write_text("SECRET=test\n")
    (project / ".env").symlink_to(outside)
    with pytest.raises(PodgroveError, match="outside the worktree"):
        load_config(project)


def test_schema_matches_installed_validation():
    schema = Path(__file__).resolve().parents[1] / "schema" / "podgrove-v1.schema.json"
    assert json.loads(schema.read_text()) == CONFIG_SCHEMA


def test_compose_project_directory_inside_sync_root(project):
    (project / "tests").mkdir()
    (project / "podgrove.yml").write_text("compose:\n  project_directory: tests\n")
    assert load_config(project).project_directory == project / "tests"
    (project / "podgrove.yml").write_text("compose:\n  project_directory: ..\n")
    with pytest.raises(PodgroveError, match="project_directory"):
        load_config(project)


def test_default_discovery_includes_compose_override_but_explicit_files_do_not(project):
    (project / "compose.override.yml").write_text("services: {}\n")
    (project / "compose.override.yaml").write_text("services: {}\n")
    assert load_config(project).files == [project / "compose.yaml", project / "compose.override.yml"]
    assert load_config(project, files=["compose.yaml"]).files == [project / "compose.yaml"]
    (project / "podgrove.yml").write_text("compose:\n  files: [compose.yaml]\n")
    assert load_config(project).files == [project / "compose.yaml"]


def test_default_discovery_starts_at_compose_project_directory(project):
    nested = project / "tests"
    nested.mkdir()
    (nested / "docker-compose.yml").write_text("services: {}\n")
    (project / "podgrove.yml").write_text("compose:\n  project_directory: tests\n")
    assert load_config(project).files == [nested / "docker-compose.yml"]
    (nested / "docker-compose.yml").unlink()
    assert load_config(project).files == [project / "compose.yaml"]


def test_huge_ttl_is_rejected_before_runtime(project):
    (project / "podgrove.yml").write_text("ttl: " + "9" * 5000 + "h\n")
    with pytest.raises(PodgroveError, match="duration is too large"):
        load_config(project)


@pytest.mark.parametrize("mode", ["shared", "tainted"])
def test_node_mode_configuration(project, mode):
    (project / "podgrove.yml").write_text(f"node_mode: {mode}\n")
    assert load_config(project).node_mode == mode


def test_doctor_can_load_config_before_compose_project_exists(tmp_path):
    (tmp_path / "podgrove.yml").write_text("node_mode: tainted\n")
    config = load_config(tmp_path, require_compose=False)
    assert config.files == []
    assert config.node_mode == "tainted"
    with pytest.raises(PodgroveError, match="compose.files"):
        load_config(tmp_path)


def test_optional_compose_mode_still_validates_explicit_files(tmp_path):
    with pytest.raises(PodgroveError, match="compose.files"):
        load_config(tmp_path, files=["missing.yml"], require_compose=False)
    (tmp_path / "podgrove.yml").write_text("compose:\n  files: [missing.yml]\n")
    with pytest.raises(PodgroveError, match="compose.files"):
        load_config(tmp_path, require_compose=False)


def test_tainted_node_settings_merge_optional_fields(project):
    (project / "podgrove.yml").write_text(
        "node_mode: tainted\ntainted_nodes:\n  selector:\n    company.example/node-pool: dev\n"
        "  taint:\n    key: company.example/workload\n    value: ''\n"
    )
    config = load_config(project)
    assert config.tainted_nodes == {
        "selector": {"company.example/node-pool": "dev"},
        "taint": {"key": "company.example/workload", "value": "", "effect": "NoSchedule"},
    }
    (project / "podgrove.yml").write_text("tainted_nodes:\n  taint:\n    effect: NoExecute\n")
    expected = default_tainted_nodes()
    expected["taint"]["effect"] = "NoExecute"
    assert load_config(project).tainted_nodes == expected


def test_quoted_empty_node_label_value_is_supported(project):
    (project / "podgrove.yml").write_text(
        "node_mode: tainted\ntainted_nodes:\n  selector: {example.com/test-node: ''}\n"
        "  taint: {key: example.com/test-only, value: ''}\n"
    )
    config = load_config(project)
    assert config.tainted_nodes["selector"] == {"example.com/test-node": ""}
    assert config.tainted_nodes["taint"]["value"] == ""


def test_tainted_node_defaults_are_not_shared_between_config_instances(project):
    first = Config(project, [])
    second = Config(project, [])
    first.tainted_nodes["taint"]["value"] = "changed"
    first.tainted_nodes["selector"]["pool"] = "changed"
    assert second.tainted_nodes == default_tainted_nodes()


@pytest.mark.parametrize("setting", [
    "selector: {'/pool': dev}",
    "selector: {'bad domain/pool': dev}",
    "selector: {'pool/': dev}",
    "selector: {pool: 'bad value'}",
    "selector: {pool: '" + "x" * 64 + "'}",
    "selector: {eks.amazonaws.com/compute-type: fargate}",
    "selector: {eks.amazonaws.com/compute-type: auto}",
    "taint: {key: 'bad/key/extra'}",
    "taint: {value: 'bad value'}",
    "taint: {key: 'example.com/'}",
])
def test_tainted_node_label_and_taint_syntax_validated_before_runtime(project, setting):
    (project / "podgrove.yml").write_text(f"tainted_nodes:\n  {setting}\n")
    with pytest.raises(PodgroveError, match="tainted_nodes"):
        load_config(project)


@pytest.mark.parametrize("cluster,target", [
    ("{}", {"context": None, "namespace": None}),
    ("{context: 'fixture:development'}", {"context": "fixture:development", "namespace": None}),
    ("{namespace: default}", {"context": None, "namespace": "default"}),
    ("{namespace: my-development}", {"context": None, "namespace": "my-development"}),
    ("{namespace: a}", {"context": None, "namespace": "a"}),
    ("{namespace: '" + "a" * 63 + "'}", {"context": None, "namespace": "a" * 63}),
    ("{context: dev, namespace: podgrove-testing}", {"context": "dev", "namespace": "podgrove-testing"}),
    ("{context: 'arn:aws:eks:us-east-1:123:cluster/dev', namespace: wt-example}",
     {"context": "arn:aws:eks:us-east-1:123:cluster/dev", "namespace": "wt-example"}),
])
def test_optional_cluster_fields_match_target_and_full_configuration(project, cluster, target):
    (project / "podgrove.yml").write_text(f"cluster: {cluster}\n")
    assert load_target(project) == target
    config = load_config(project)
    assert {"context": config.context, "namespace": config.namespace} == target


@pytest.mark.parametrize("text,error", [
    ("cluster: {contex: dev}", "contex"),
    ("cluster: {context: dev, namespaces: [default]}", "namespaces"),
    ("cluster: null", "cluster"),
    ("cluster: []", "cluster"),
    ("cluster: {context: true}", "cluster.context"),
    ("cluster: {context: 123}", "cluster.context"),
    ("cluster: {context: null}", "cluster.context"),
    ("cluster: {context: ''}", "cluster.context"),
    ("cluster: {context: '   '}", "cluster.context"),
    ('cluster: {context: "bad\\ncontext"}', "cluster.context"),
    ('cluster: {context: "bad\\u0000context"}', "cluster.context"),
    ('cluster: {context: "bad\\u007fcontext"}', "cluster.context"),
    ("cluster: {context: '" + "x" * 513 + "'}", "cluster.context"),
    ("cluster: {namespace: ''}", "cluster.namespace"),
    ("cluster: {namespace: true}", "cluster.namespace"),
    ("cluster: {namespace: bad.name}", "cluster.namespace"),
    ("cluster: {namespace: ../another}", "cluster.namespace"),
    ("cluster: {namespace: wt-Example}", "cluster.namespace"),
    ("cluster: {namespace: wt-name-}", "cluster.namespace"),
    ("cluster: {namespace: 'wt-" + "x" * 61 + "'}", "cluster.namespace"),
    ("cluster: {context: first, context: second}", "duplicate"),
    ("cluster: {namespace: default, namespace: podgrove-testing}", "duplicate"),
    ("cluster: {}\ncluster: {}", "duplicate"),
    ("cluster: {namespace_mode: exclusive}", "cluster.namespace_mode"),
    ("cluster: {namespace_mode: true}", "cluster.namespace_mode"),
    ("cluster: {storage_class: ''}", "cluster.storage_class"),
    ("cluster: {storage_class: ../disk}", "cluster.storage_class"),
    ("cluster: {storage_class: 'Class!'}", "cluster.storage_class"),
    ("cluster: {storage_class: true}", "cluster.storage_class"),
])
@pytest.mark.parametrize("loader", [load_target, load_config], ids=["target", "stack"])
def test_invalid_cluster_configuration_is_rejected_before_target_use(project, text, error, loader):
    (project / "podgrove.yml").write_text(text)
    with pytest.raises(PodgroveError, match=error):
        loader(project)


def test_target_reader_needs_no_stack_and_ignores_context_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PODGROVE_CONTEXT", "environment-does-not-implicitly-select-a-cluster")
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "missing-kubeconfig"))
    before = set(tmp_path.iterdir())
    assert load_target(tmp_path) == {"context": None, "namespace": None}
    assert set(tmp_path.iterdir()) == before
    (tmp_path / "podgrove.yml").write_text("")
    assert load_target(tmp_path) == {"context": None, "namespace": None}


def test_target_reader_never_resolves_stack_paths_executes_or_creates_state(tmp_path, monkeypatch):
    selected = tmp_path / "podgrove.yml"
    selected.write_text(
        "cluster: {context: dev, namespace: default}\n"
        "compose:\n  files: [missing-compose.yaml]\n  env_file: ../private.env\n"
        "  project_directory: missing-stack-directory\n"
    )
    original_checked = config_module.checked_path
    original_read = Path.read_text

    def only_configuration_path(root, value, key, **kwargs):
        assert key == "config", "Target selection attempted to resolve a stack path"
        return original_checked(root, value, key, **kwargs)

    def only_configuration_read(path, *args, **kwargs):
        assert path == selected, "Target selection attempted another file read"
        return original_read(path, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Target selection attempted a command or directory creation")

    monkeypatch.setattr(config_module, "checked_path", only_configuration_path)
    monkeypatch.setattr(Path, "read_text", only_configuration_read)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    assert load_target(tmp_path) == {"context": "dev", "namespace": "default"}
    assert set(tmp_path.iterdir()) == {selected}


def test_target_custom_configuration_is_selected_relative_to_worktree(tmp_path, monkeypatch):
    other = tmp_path / "cwd"
    root = tmp_path / "worktree"
    other.mkdir()
    root.mkdir()
    (other / "podgrove.yml").write_text("cluster: {context: wrong}\n")
    (root / "podgrove.yml").write_text("cluster: {context: default-config}\n")
    (root / "chosen.yml").write_text("cluster: {context: chosen, namespace: wt-test}\n")
    monkeypatch.chdir(other)
    assert load_target(root, Path("chosen.yml")) == {"context": "chosen", "namespace": "wt-test"}
    with pytest.raises(PodgroveError, match="path does not exist"):
        load_target(root, Path("not-here.yml"))
    with pytest.raises(PodgroveError, match="outside the worktree"):
        load_target(root, other / "podgrove.yml")
    with pytest.raises(PodgroveError, match=".git paths"):
        load_target(root, Path(".git/config.yml"))


def test_target_configuration_symlink_cannot_escape_root(tmp_path):
    root = tmp_path / "worktree"
    root.mkdir()
    outside = tmp_path / "private.yml"
    outside.write_text("cluster: {context: wrong}\n")
    (root / "podgrove.yml").symlink_to(outside)
    with pytest.raises(PodgroveError, match="outside the worktree"):
        load_target(root)


@pytest.mark.parametrize("text,error", [
    ("size: huge", "size"),
    ("forward: [{service: app, port: 80}, {service: app, port: 80, local: 4000}]", "duplicate"),
    ("forward: [{service: app, port: 80, local: 4000}, {service: db, port: 80, local: 4000}]", "duplicate"),
    ("ttl: 9007199254740993s", "duration is too large"),
    ("tainted_nodes: {selector: {'bad/key/extra': dev}}", "tainted_nodes"),
])
def test_target_reader_reuses_non_path_semantic_validation_without_compose(tmp_path, text, error):
    (tmp_path / "podgrove.yml").write_text(text + "\ncluster: {context: dev}\n")
    with pytest.raises(PodgroveError, match=error):
        load_target(tmp_path)


@pytest.mark.parametrize("mode", ["shared", "worktree"])
def test_cluster_settings_are_portable_and_bootstrap_needs_no_compose(project, mode):
    cluster = {"context": "another-provider", "namespace": "my-team-dev", "namespace_mode": mode,
               "storage_class": "fast.csi-storage"}
    import yaml
    selected = project / "podgrove.yml"
    selected.write_text(yaml.safe_dump({"cluster": cluster}))
    assert load_cluster(project) == cluster
    config = load_config(project)
    assert config.namespace_mode == mode and config.storage_class == "fast.csi-storage"
    assert config.context == "another-provider" and config.namespace == "my-team-dev"
    selected.write_text(yaml.safe_dump({"cluster": cluster, "compose": {"files": ["missing.yml"]}}))
    assert load_cluster(project) == cluster
    with pytest.raises(PodgroveError, match="path does not exist"):
        load_config(project)
