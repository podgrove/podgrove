"""The small, versioned configuration around an unchanged Compose project."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .errors import PodgroveError
from .network import network_settings
from .placement import PLACEMENT_SCHEMA, placement_spec, validate_placement
from .sync_filter import validate_patterns
from .resources import QUANTITY_SCHEMA, RESOURCE_SCHEMA, quantity_text, resource_budget


# Kept in Python so the installed CLI does not depend on a source checkout.
# tests/test_config.py verifies the published JSON schema is identical.
CONFIG_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://podgrove.dev/schema/podgrove-v1.schema.json",
    "title": "Podgrove configuration, version 1",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "version": {"type": "integer", "const": 1, "default": 1},
        "cluster": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "context": {
                    "type": "string", "minLength": 1, "maxLength": 512,
                    "pattern": r"^[^\x00-\x1f\x7f]+$",
                    "description": "Explicit kubeconfig context; never falls back to the current context.",
                },
                "namespace": {
                    "type": "string", "minLength": 1, "maxLength": 63,
                    "pattern": r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
                    "description": "Required deployment namespace; in worktree mode this is the namespace base.",
                },
                "namespace_mode": {
                    "type": "string", "enum": ["shared", "worktree"], "default": "shared",
                    "description": "Use the configured namespace, or derive a separate namespace per worktree.",
                },
                "storage_class": {
                    "type": "string", "minLength": 1, "maxLength": 253,
                    "pattern": r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$",
                    "description": "Approved dynamic StorageClass, shared by bootstrap rendering and environment startup.",
                },
            },
        },
        "compose": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "files": {
                    "type": "array", "minItems": 1, "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "profiles": {
                    "type": "array", "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "env_file": {"type": "string", "minLength": 1},
                "project_directory": {
                    "type": "string", "minLength": 1,
                    "description": "Compose path-resolution base inside the worktree; defaults to its root.",
                },
            },
        },
        "forward": {
            "type": "array", "uniqueItems": True,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["service", "port"],
                "properties": {
                    "service": {"type": "string", "minLength": 1},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                    "local": {"type": "integer", "minimum": 1, "maximum": 65535},
                },
            },
        },
        "reverse": {
            "type": "array", "maxItems": 32,
            "items": {"type": "object", "additionalProperties": False, "required": ["local_port"],
                      "properties": {
                          "local_port": {"type": "integer", "minimum": 1, "maximum": 65535},
                          "remote_port": {"type": "integer", "minimum": 1024, "maximum": 65535,
                                          "description": "Engine-side listener port; defaults to local_port when at least 1024."},
                          "local_host": {"type": "string", "enum": ["127.0.0.1", "::1"], "default": "127.0.0.1"},
                      }},
        },
        "connect": {
            "type": "array", "maxItems": 32,
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["name", "environment", "service", "port"],
                      "properties": {
                          "name": {"type": "string", "minLength": 1, "maxLength": 63,
                                   "pattern": r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"},
                          "environment": {"type": "string", "pattern": r"^[a-f0-9]{12}$"},
                          "service": {"type": "string", "minLength": 1, "maxLength": 128,
                                      "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"},
                          "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                      }},
        },
        "sync": {
            "type": "object", "additionalProperties": False,
            "properties": {"exclude": {"type": "array", "maxItems": 128, "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 256},
                "description": "Optional workspace-relative globs excluded from bind/config/secret mirrors; does not change Compose build or watch behavior."}},
        },
        "size": {"type": "string", "enum": ["small", "medium", "large"], "default": "medium"},
        "resources": {**RESOURCE_SCHEMA, "description": "Exact engine resource budget; replaces size when present. Omitted dimensions are not inherited."},
        "init_resources": {**RESOURCE_SCHEMA, "description": "Exact storage initializer resource budget; replaces initializer defaults when present."},
        "storage": {"type": "object", "additionalProperties": False,
                    "properties": {"size": {**QUANTITY_SCHEMA, "default": "20Gi"}}},
        "node_mode": {"type": "string", "enum": ["shared", "tainted"], "default": "shared"},
        "placement": PLACEMENT_SCHEMA,
        "network": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "blocked_cidrs": {
                    "type": "array", "maxItems": 128, "uniqueItems": True,
                    "items": {"type": "string", "minLength": 3, "maxLength": 49},
                    "description": "Additional infrastructure CIDRs excluded from public web egress; built-in exclusions always remain.",
                },
            },
        },
        "tainted_nodes": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "selector": {
                    "type": "object", "minProperties": 1,
                    "additionalProperties": {"type": "string"},
                },
                "taint": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "key": {"type": "string", "minLength": 1},
                        "value": {"type": "string"},
                        "effect": {"type": "string", "enum": ["NoSchedule", "NoExecute"]},
                    },
                },
            },
        },
        "ttl": {"type": "string", "pattern": "^[1-9][0-9]*[smhd]$", "default": "8h"},
    },
}


class _UniqueLoader(yaml.SafeLoader):
    """Catch misspellings hidden by duplicate YAML keys."""


def _unique_mapping(loader: _UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise PodgroveError("podgrove.yml keys must be strings")
        if key in result:
            raise PodgroveError(f"podgrove.yml has duplicate key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def default_tainted_nodes() -> dict:
    return {"selector": {"podgrove.dev/dedicated": "true"},
            "taint": {"key": "dedicated", "value": "podgrove", "effect": "NoSchedule"}}


def normalize_reverse(value: list | None) -> list[dict]:
    entries = [] if value is None else value
    errors = list(jsonschema.Draft202012Validator(CONFIG_SCHEMA["properties"]["reverse"]).iter_errors(entries))
    if errors:
        raise PodgroveError(f"reverse: {errors[0].message}")
    result, ports = [], set()
    for entry in entries:
        remote = entry.get("remote_port", entry["local_port"])
        if remote < 1024 or remote in (2375, 2376):
            raise PodgroveError("reverse.remote_port: choose a port from 1024 to 65535 other than Docker API ports 2375 and 2376")
        if remote in ports:
            raise PodgroveError("reverse.remote_port: duplicate engine listener port")
        ports.add(remote)
        result.append({"local_host": entry.get("local_host", "127.0.0.1"),
                       "local_port": entry["local_port"], "remote_port": remote})
    return result


def normalize_connect(value: list | None) -> list[dict]:
    entries = [] if value is None else value
    errors = list(jsonschema.Draft202012Validator(CONFIG_SCHEMA["properties"]["connect"]).iter_errors(entries))
    if errors:
        raise PodgroveError(f"connect: {errors[0].message}")
    result, names = [], set()
    reserved = {"localhost", "host", "docker", "podgrove", "host-docker-internal", "gateway-docker-internal"}
    for entry in entries:
        name = entry["name"]
        if name in reserved:
            raise PodgroveError("connect.name: reserved host or Docker alias")
        if name in names:
            raise PodgroveError("connect.name: duplicate connection alias")
        if entry["port"] in (2375, 2376):
            raise PodgroveError("connect.port: Docker API ports 2375 and 2376 are reserved")
        names.add(name)
        result.append(dict(entry))
    return result


def storage_class_name(value: str) -> str:
    """Validate CLI overrides using the same DNS-subdomain rules as YAML."""
    rule = CONFIG_SCHEMA["properties"]["cluster"]["properties"]["storage_class"]
    if not jsonschema.Draft202012Validator(rule).is_valid(value):
        raise PodgroveError("storage_class must be a nonempty Kubernetes DNS subdomain (at most 253 characters)")
    return value


@dataclass
class Config:
    root: Path
    files: list[Path]
    profiles: list[str] = field(default_factory=list)
    env_file: Path | None = None
    forward: list[dict] | None = None
    size: str = "medium"
    ttl_seconds: int = 8 * 3600
    project_directory: Path | None = None
    node_mode: str = "shared"
    tainted_nodes: dict = field(default_factory=default_tainted_nodes)
    context: str | None = None
    namespace: str | None = None
    namespace_mode: str = "shared"
    storage_class: str | None = None
    resources: dict | None = None
    init_resources: dict | None = None
    storage_size: str = "20Gi"
    network: dict = field(default_factory=network_settings)
    sync_exclude: list[str] = field(default_factory=list)
    placement: dict = field(default_factory=dict)
    reverse: list[dict] = field(default_factory=list)
    connect: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.project_directory is None:
            self.project_directory = self.root


def checked_path(root: Path, value: str | Path, key: str, *, required: bool = True) -> Path:
    """Return an absolute lexical path after checking its resolved destination.

    Preserve the lexical path: Compose sends it to the remote daemon verbatim.
    Resolving it in the returned value would silently change a bind's source.
    """
    raw = Path(value)
    path = Path(os.path.abspath(root / raw)) if not raw.is_absolute() else Path(os.path.abspath(raw))
    try:
        relative = path.relative_to(root)
        resolved = path.resolve(strict=False)
        resolved_relative = resolved.relative_to(root)
    except (ValueError, OSError, RuntimeError) as exc:
        raise PodgroveError(f"{key}: path is outside the worktree or has an unsafe symlink: {value}") from exc
    if ".git" in relative.parts or ".git" in resolved_relative.parts:
        raise PodgroveError(f"{key}: .git paths cannot be mounted or synced: {value}")
    if required and not path.exists():
        raise PodgroveError(f"{key}: path does not exist: {value}")
    return path


def _configuration_data(root: Path, config_path: Path | None) -> tuple[Path, dict]:
    """Read only the selected Podgrove file, without following stack references."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise PodgroveError(f"Project directory does not exist: {root}")
    selected = config_path or Path("podgrove.yml")
    selected = checked_path(root, selected, "config", required=config_path is not None)
    data: Any = {}
    if selected.exists():
        try:
            data = yaml.load(selected.read_text(encoding="utf-8"), Loader=_UniqueLoader)
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise PodgroveError(f"Cannot read {selected}: {exc}") from exc
        if data is None:
            data = {}
    errors = sorted(jsonschema.Draft202012Validator(CONFIG_SCHEMA).iter_errors(data), key=lambda e: str(e.path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "podgrove.yml"
        raise PodgroveError(f"{location}: {error.message}")
    return root, data


def _validate_settings(data: dict) -> tuple[int, dict]:
    """Validate semantic settings without opening any referenced filesystem path."""
    from .kube import context_name, namespace_name, validate_tainted_nodes

    cluster = data.get("cluster", {})
    for key, validate in (("context", context_name), ("namespace", namespace_name)):
        if key in cluster:
            try:
                validate(cluster[key])
            except PodgroveError as exc:
                raise PodgroveError(f"cluster.{key}: {exc}") from exc
    tainted_nodes = default_tainted_nodes()
    configured_taints = data.get("tainted_nodes", {})
    if "selector" in configured_taints:
        tainted_nodes["selector"] = configured_taints["selector"]
    tainted_nodes["taint"].update(configured_taints.get("taint", {}))
    validate_tainted_nodes(tainted_nodes)
    placement_spec(data.get("placement"), node_mode=data.get("node_mode", "shared"), tainted_nodes=tainted_nodes)
    normalize_reverse(data.get("reverse"))
    normalize_connect(data.get("connect"))
    network_settings(data.get("network"))
    validate_patterns(data.get("sync", {}).get("exclude", []))
    for section in ("resources", "init_resources"):
        if section in data:
            resource_budget(data[section], section)
    quantity_text(data.get("storage", {}).get("size", "20Gi"), "storage.size")

    duration = data.get("ttl", "8h")
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", duration)
    assert match is not None  # Already constrained by the schema.
    try:
        ttl_seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
        # Runtime lifecycle timestamps use float seconds; reject unrepresentable
        # input here, rather than creating an environment the reaper cannot read.
        if ttl_seconds > 2**53:
            raise ValueError
    except ValueError as exc:
        raise PodgroveError("ttl: duration is too large; maximum is 9007199254740992 seconds") from exc
    forwards = data.get("forward")
    if forwards is not None:
        destinations = [(entry["service"], entry["port"]) for entry in forwards]
        if len(set(destinations)) != len(destinations):
            raise PodgroveError("forward: duplicate service/port target")
        local_ports = [entry["local"] for entry in forwards if "local" in entry]
        if len(set(local_ports)) != len(local_ports):
            raise PodgroveError("forward.local: duplicate local port")
    return ttl_seconds, tainted_nodes


def load_cluster(root: Path, config_path: Path | None = None) -> dict[str, str]:
    """Read deployment settings without resolving Compose or secret source files."""
    _, data = _configuration_data(root, config_path)
    _validate_settings(data)
    return dict(data.get("cluster", {}))


def load_target(root: Path, config_path: Path | None = None) -> dict[str, str | None]:
    """Resolve explicit cluster metadata without opening Compose or environment files.

    Missing default configuration leaves both fields unset. An explicitly chosen
    missing configuration is an error, as it is for full stack configuration.
    This function reads no kubeconfig, creates no state, and invokes no commands.
    """
    cluster = load_cluster(root, config_path)
    return {"context": cluster.get("context"), "namespace": cluster.get("namespace")}


def load_config(
    root: Path, config_path: Path | None = None, files: list[str] | None = None, *, require_compose: bool = True,
) -> Config:
    root, data = _configuration_data(root, config_path)
    ttl_seconds, tainted_nodes = _validate_settings(data)
    compose = data.get("compose", {})
    project_directory = checked_path(root, compose.get("project_directory", "."), "compose.project_directory")
    if not project_directory.is_dir():
        raise PodgroveError(f"compose.project_directory: expected a directory: {project_directory}")
    names = files if files is not None else compose.get("files")
    allow_no_compose = not require_compose and names is None
    if names is None:
        candidates = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
        overrides = ("compose.override.yml", "compose.override.yaml",
                     "docker-compose.override.yml", "docker-compose.override.yaml")
        search = project_directory
        while True:
            found = next((search / name for name in candidates if (search / name).is_file()), None)
            if found is not None:
                names = [str(found)]
                override = next((search / name for name in overrides if (search / name).is_file()), None)
                if override is not None:
                    names.append(str(override))
                break
            if search == root:
                # Doctor can validate cluster settings before a stack exists.
                names = [] if allow_no_compose else ["compose.yaml"]
                break
            search = search.parent
    if not names and not allow_no_compose:
        raise PodgroveError("compose.files: at least one Compose file is required")
    paths = [checked_path(root, name, "compose.files") for name in names]
    for path in paths:
        if not path.is_file():
            raise PodgroveError(f"compose.files: expected a regular file: {path}")
    env_file = None
    if "env_file" in compose:
        env_file = checked_path(root, compose["env_file"], "compose.env_file")
        if not env_file.is_file():
            raise PodgroveError(f"compose.env_file: expected a regular file: {env_file}")
    # Compose automatically reads .env files, even without an explicit --env-file.
    for directory in {root, project_directory}:
        if (directory / ".env").exists() or (directory / ".env").is_symlink():
            checked_path(root, directory / ".env", "compose default .env")
    return Config(
        root=root, files=paths, profiles=compose.get("profiles", []), env_file=env_file,
        forward=data.get("forward"), size=data.get("size", "medium"), ttl_seconds=ttl_seconds,
        project_directory=project_directory,
        node_mode=data.get("node_mode", "shared"),
        tainted_nodes=tainted_nodes,
        context=data.get("cluster", {}).get("context"), namespace=data.get("cluster", {}).get("namespace"),
        namespace_mode=data.get("cluster", {}).get("namespace_mode", "shared"),
        storage_class=data.get("cluster", {}).get("storage_class"),
        resources=resource_budget(data["resources"]) if "resources" in data else None,
        init_resources=resource_budget(data["init_resources"], "init_resources") if "init_resources" in data else None,
        storage_size=quantity_text(data.get("storage", {}).get("size", "20Gi"), "storage.size"),
        network=network_settings(data.get("network")),
        sync_exclude=data.get("sync", {}).get("exclude", []),
        placement=validate_placement(data.get("placement")),
        reverse=normalize_reverse(data.get("reverse")),
        connect=normalize_connect(data.get("connect")),
    )
