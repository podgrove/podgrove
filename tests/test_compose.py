import json
import shutil
from unittest.mock import patch

import pytest

from podgrove.compose import Compose
from podgrove.config import load_config
from podgrove.errors import PodgroveError


@pytest.fixture
def project(tmp_path):
    (tmp_path / "compose.yaml").write_text("services:\n  app:\n    image: busybox:1.37\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n")
    (tmp_path / "config.txt").write_text("test config\n")
    return Compose(load_config(tmp_path))


def base_model():
    return {"services": {"app": {"image": "busybox:1.37", "ports": [{"target": 8080, "published": "8000"}]}}}


def test_command_preserves_file_order_and_project_directory(project):
    project.config.files.append(project.config.root / "nested" / "override.yml")
    project.config.profiles = ["test", "debug"]
    project.config.env_file = project.config.root / ".env.test"
    command = project.command("up", "--detach")
    assert command == [
        "docker", "compose", "--ansi", "never", "--project-directory", str(project.config.root),
        "--file", str(project.config.files[0]), "--file", str(project.config.files[1]),
        "--profile", "test", "--profile", "debug", "--env-file", str(project.config.env_file), "up", "--detach",
    ]


def test_model_uses_compose_json_and_preserves_optional_env_metadata(project):
    model = base_model()
    model["services"]["app"]["env_file"] = [{"path": str(project.config.root / "optional.env"), "required": False}]
    with patch("podgrove.compose.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps(model)
        assert project.model() == model
        assert run.call_args.args[0][-4:] == ["config", "--format", "json", "--no-env-resolution"]


@pytest.mark.parametrize(("field", "value"), [
    ("network_mode", "host"), ("network_mode", "container:someone-else"),
    ("pid", "host"), ("ipc", "host"), ("uts", "host"), ("cgroup", "host"),
    ("devices", ["/dev/video0"]), ("gpus", "all"), ("device_cgroup_rules", ["c 1:3 mrw"]),
    ("external_links", ["other:db"]), ("volumes_from", ["other"]),
    ("use_api_socket", True), ("cgroup_parent", "/system.slice"),
    ("provider", {"type": "external-cloud"}), ("models", {"assistant": {"model": "ai/example"}}),
])
def test_host_coupled_service_keys_refused(project, field, value):
    model = base_model()
    model["services"]["app"][field] = value
    with pytest.raises(PodgroveError, match=f"services.app.{field}"):
        project.validate(model)


@pytest.mark.parametrize("kind", ["networks", "volumes", "configs", "secrets"])
def test_external_resources_refused(project, kind):
    model = base_model()
    model[kind] = {"existing": {"external": True}}
    with pytest.raises(PodgroveError, match=f"{kind}.existing.external"):
        project.validate(model)


@pytest.mark.parametrize(("kind", "definition", "key"), [
    ("volumes", {"driver": "custom"}, "driver"),
    ("volumes", {"driver_opts": {"device": "/private/data", "type": "none", "o": "bind"}}, "driver_opts"),
    ("networks", {"driver": "host"}, "driver"),
])
def test_host_driver_refused(project, kind, definition, key):
    model = base_model()
    model[kind] = {"host": definition}
    with pytest.raises(PodgroveError, match=f"{kind}.host.{key}"):
        project.validate(model)


def test_usual_compose_semantics_preserved(project):
    model = base_model()
    app = model["services"]["app"]
    app.update({
        "depends_on": {"redis": {"condition": "service_healthy"}},
        "healthcheck": {"test": ["CMD", "true"]}, "restart": "unless-stopped",
        "networks": {"app": {"aliases": ["api"]}}, "init": True,
        "mem_limit": "512M", "cpus": 0.5, "user": "1000:1000",
        "volumes": [{"type": "volume", "source": "data", "target": "/data"}],
        "tmpfs": ["/tmp"], "environment": {"MODE": "test"},
    })
    model["services"]["redis"] = {"image": "redis:7-alpine"}
    model["networks"] = {"app": {"name": "my-app", "driver": "bridge"}}
    model["volumes"] = {"data": {"name": "my-data", "driver": "local"}}
    original = json.dumps(model, sort_keys=True)
    project.validate(model)
    assert json.dumps(model, sort_keys=True) == original


def test_sync_roots_include_file_binds_configs_secrets_and_deduplicate(project):
    root = project.config.root
    model = base_model()
    model["services"]["app"]["volumes"] = [
        {"type": "bind", "source": str(root / "src"), "target": "/app"},
        {"type": "bind", "source": str(root / "src" / "app.py"), "target": "/entry.py"},
    ]
    model["configs"] = {"cfg": {"file": str(root / "config.txt")}}
    model["secrets"] = {"test-secret": {"file": str(root / "config.txt")}, "env-secret": {"environment": "TEST"}}
    project.validate(model)
    assert project.sync_paths(model) == [root / "config.txt", root / "src"]


@pytest.mark.parametrize(("source", "message"), [
    ("missing", "does not exist"), ("../outside", "outside the worktree"), (".git/config", ".git paths"),
])
def test_bad_bind_sources_refused(project, source, message):
    model = base_model()
    model["services"]["app"]["volumes"] = [{"type": "bind", "source": source, "target": "/app"}]
    with pytest.raises(PodgroveError, match=message):
        project.validate(model)


def test_symlink_within_sync_root_refused_and_git_skipped(project):
    root = project.config.root
    (root / "src" / ".git").symlink_to("/does-not-exist")
    model = base_model()
    model["services"]["app"]["volumes"] = [{"type": "bind", "source": str(root / "src"), "target": "/app"}]
    project.validate(model)
    (root / "src" / "link").symlink_to("app.py")
    with pytest.raises(PodgroveError, match="symlinks"):
        project.validate(model)


def test_develop_watch_validated_but_not_mirrored(project):
    model = base_model()
    model["services"]["app"]["develop"] = {"watch": [{"path": str(project.config.root / "src"),
                                                       "target": "/app", "action": "sync"}]}
    project.validate(model)
    assert project.has_watch(model)
    assert project.sync_paths(model) == []
    model["services"]["app"]["develop"]["watch"][0]["path"] = "/etc/passwd"
    with pytest.raises(PodgroveError, match="develop.watch.path"):
        project.validate(model)


def test_env_file_required_and_optional(project):
    model = base_model()
    model["services"]["app"]["env_file"] = [{"path": "missing.env", "required": False}]
    project.validate(model)
    model["services"]["app"]["env_file"][0]["required"] = True
    with pytest.raises(PodgroveError, match="env_file"):
        project.validate(model)


def test_ports_dynamic_range_and_invalid_protocol(project):
    model = base_model()
    model["services"]["app"]["ports"] = [{"target": 8080}, {"target": 8081, "published": "30000-30100"}]
    with pytest.raises(PodgroveError, match="explicit published port"):
        project.validate(model)
    project.config.forward = []
    project.validate(model)
    assert project.published_ports(model) == [
        {"service": "app", "target": 8080, "published": 0, "protocol": "tcp"},
        {"service": "app", "target": 8081, "published": "30000-30100", "protocol": "tcp"},
    ]
    model["services"]["app"]["ports"][0]["protocol"] = "udp"
    with pytest.raises(PodgroveError, match="ports.protocol"):
        project.validate(model)


@pytest.mark.parametrize("published", ["-1", "0-4", "70000", "10-5", "abc"])
def test_bad_published_ports(project, published):
    model = base_model()
    model["services"]["app"]["ports"][0]["published"] = published
    with pytest.raises(PodgroveError, match="ports"):
        project.validate(model)


@pytest.mark.parametrize("published", ["2375", "2000-3000"])
def test_docker_api_port_is_reserved_even_without_forwarding(project, published):
    model = base_model()
    project.config.forward = []
    model["services"]["app"]["ports"][0]["published"] = published
    with pytest.raises(PodgroveError, match="reserved for"):
        project.validate(model)


@pytest.mark.parametrize(("service", "port", "key"), [("missing", 8080, "forward.service"), ("app", 80, "forward.port")])
def test_invalid_forward_target(project, service, port, key):
    project.config.forward = [{"service": service, "port": port}]
    with pytest.raises(PodgroveError, match=key):
        project.validate(base_model())


def test_forward_replica_ambiguity_and_explicit_disable(project):
    model = base_model()
    model["services"]["app"]["deploy"] = {"replicas": 2}
    with pytest.raises(PodgroveError, match="replicas"):
        project.validate(model)
    project.config.forward = []
    project.validate(model)


def test_build_context_validated_and_environment_secret_allowed(project):
    root = project.config.root
    (root / "Dockerfile").write_text("FROM busybox:1.37\n")
    model = base_model()
    model["services"]["app"]["build"] = {"context": str(root), "secrets": [{"source": "token"}]}
    model["secrets"] = {"token": {"environment": "TEST_TOKEN"}}
    project.validate(model)
    model["services"]["app"]["build"]["context"] = str(root.parent)
    with pytest.raises(PodgroveError, match="build.context"):
        project.validate(model)


@pytest.mark.parametrize("extra", ["include: other.yml\n", "services:\n  app:\n    extends: {file: other.yml, service: app}\n"])
def test_indirect_source_files_refused_before_compose_subprocess(project, extra):
    project.config.files[0].write_text(extra)
    with patch("podgrove.compose.subprocess.run") as run:
        with pytest.raises(PodgroveError, match="include|extends"):
            project.model()
        run.assert_not_called()


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose CLI required; daemon not needed")
def test_real_compose_overlay_tags_profiles_paths_and_optional_env(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "first").write_text("present before startup\n")
    (tmp_path / "config.txt").write_text("test config")
    (tmp_path / "nested").mkdir()
    source = (
        "name: podgrove-compose-unit\nservices:\n  app:\n    image: busybox:1.37\n"
        "    volumes: ['./src:/app:ro']\n    ports: ['8000:80']\n"
        "    env_file:\n      - path: optional.env\n        required: false\n"
        "    configs: [settings]\n  debug:\n    image: busybox:1.37\n    profiles: [debug]\n"
        "configs:\n  settings:\n    file: ./config.txt\n"
    )
    overlay = "services:\n  app:\n    ports: !override ['9000:80']\n"
    (tmp_path / "compose.yaml").write_text(source)
    (tmp_path / "nested" / "override.yml").write_text(overlay)
    compose = Compose(load_config(tmp_path, files=["compose.yaml", "nested/override.yml"]))
    model = compose.model()
    assert model["name"] == "podgrove-compose-unit"
    assert set(model["services"]) == {"app"}
    assert compose.published_ports(model) == [{"service": "app", "target": 80, "published": 9000, "protocol": "tcp"}]
    assert model["services"]["app"]["volumes"][0]["source"] == str(tmp_path / "src")
    assert set(compose.sync_paths(model)) == {tmp_path / "src", tmp_path / "config.txt"}
    assert (tmp_path / "compose.yaml").read_text() == source
    assert (tmp_path / "nested" / "override.yml").read_text() == overlay
    compose.config.profiles = ["debug"]
    assert set(compose.model()["services"]) == {"app", "debug"}


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose CLI required; daemon not needed")
def test_compose_nested_project_directory_keeps_worktree_as_boundary(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "Dockerfile").write_text("FROM busybox:1.37\n")
    (tmp_path / "tests" / "compose.yml").write_text(
        "services:\n  app:\n    build: ..\n    volumes: ['../src:/app']\n"
    )
    (tmp_path / "podgrove.yml").write_text(
        "compose:\n  files: [tests/compose.yml]\n  project_directory: tests\n"
    )
    compose = Compose(load_config(tmp_path))
    model = compose.model()
    assert model["services"]["app"]["build"]["context"] == str(tmp_path)
    assert compose.sync_paths(model) == [tmp_path / "src"]
