"""Delegate Compose semantics to Compose; validate only remote-host limitations."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .config import Config, checked_path
from .errors import PodgroveError


class _ComposeLoader(yaml.SafeLoader):
    """Read file references without interpreting Compose's merge directives."""


def _compose_tag(loader: _ComposeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(_tag, _compose_tag)


class Compose:
    def __init__(self, config: Config):
        self.config = config

    def command(self, *args: str) -> list[str]:
        command = ["docker", "compose", "--ansi", "never", "--project-directory", str(self.config.project_directory)]
        for path in self.config.files:
            command.extend(["--file", str(path)])
        for profile in self.config.profiles:
            command.extend(["--profile", profile])
        if self.config.env_file is not None:
            command.extend(["--env-file", str(self.config.env_file)])
        return [*command, *args]

    def model(self) -> dict:
        # include/extends may otherwise read files outside the worktree before
        # their origin disappears from the normalized model.
        self._check_file_indirection()
        try:
            result = subprocess.run(
                self.command("config", "--format", "json", "--no-env-resolution"),
                cwd=self.config.root, text=True, capture_output=True, timeout=60,
            )
        except FileNotFoundError as exc:
            raise PodgroveError("Docker CLI with the Compose plugin is required") from exc
        except subprocess.TimeoutExpired as exc:
            raise PodgroveError("docker compose config timed out after 60 seconds") from exc
        if result.returncode:
            raise PodgroveError(f"docker compose config failed: {result.stderr.strip() or result.stdout.strip()}")
        try:
            model = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise PodgroveError("docker compose config returned invalid JSON") from exc
        self.validate(model)
        return model

    def _check_file_indirection(self) -> None:
        for path in self.config.files:
            try:
                source = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                raise PodgroveError(f"Cannot read Compose file {path}: {exc}") from exc
            if not isinstance(source, dict):
                continue  # Compose provides the schema error.
            if source.get("include"):
                raise PodgroveError("include: unsupported in v1; pass local Compose files in compose.files")
            services = source.get("services", {})
            if isinstance(services, dict):
                for name, service in services.items():
                    if isinstance(service, dict) and service.get("extends"):
                        raise PodgroveError(f"services.{name}.extends: unsupported in v1; use Compose overlays")

    @staticmethod
    def _refuse(key: str, explanation: str) -> None:
        raise PodgroveError(f"{key}: {explanation}")

    def _local_path(self, value: str, key: str, *, required: bool = True) -> Path:
        if not isinstance(value, str) or not value:
            self._refuse(key, "expected a local path")
        return checked_path(self.config.root, value, key, required=required)

    def _check_sync_path(self, value: str, key: str, *, mirror: bool = True) -> Path:
        from .sync_filter import excluded
        path = self._local_path(value, key)
        patterns = self.config.sync_exclude if mirror else []
        if excluded(path.relative_to(self.config.root).as_posix(), patterns):
            self._refuse(key, "explicit sync source is excluded by sync.exclude")
        for component in (path, *path.parents):
            if component == self.config.root:
                break
            if component.is_symlink():
                self._refuse(key, f"symlinks in sync sources are unsupported: {component}")
        self._check_file_type(path, key)
        if path.is_dir():
            def onerror(error: OSError) -> None:
                raise PodgroveError(f"{key}: cannot scan sync source: {error}") from error

            for directory, dirs, files in os.walk(path, followlinks=False, onerror=onerror):
                dirs[:] = [entry for entry in dirs if entry != ".git" and not excluded(
                    (Path(directory) / entry).relative_to(self.config.root).as_posix(), patterns)]
                for entry in [*dirs, *files]:
                    if entry == ".git":
                        continue
                    child = Path(directory) / entry
                    if excluded(child.relative_to(self.config.root).as_posix(), patterns):
                        continue
                    self._check_file_type(child, key)
        return path

    def _check_file_type(self, path: Path, key: str) -> None:
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise PodgroveError(f"{key}: cannot read sync source {path}: {exc}") from exc
        if stat.S_ISLNK(mode):
            self._refuse(key, f"symlinks in sync sources are unsupported: {path}")
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            self._refuse(key, f"only regular files and directories can be synced: {path}")

    @staticmethod
    def _remote_context(value: str) -> bool:
        return "://" in value or value.startswith(("git@", "service:"))

    def _validate_build(self, name: str, build: Any) -> None:
        if isinstance(build, str):
            build = {"context": build}
        if not isinstance(build, dict):
            self._refuse(f"services.{name}.build", "expected a Compose build mapping")
        key = f"services.{name}.build"
        context = build.get("context", ".")
        if self._remote_context(context):
            self._refuse(f"{key}.context", "remote build contexts are unsupported in v1; use a local context")
        local_context = self._local_path(context, f"{key}.context")
        if not local_context.is_dir():
            self._refuse(f"{key}.context", "build context must be a directory")
        if "dockerfile_inline" not in build:
            dockerfile = build.get("dockerfile", "Dockerfile")
            self._local_path(str(local_context / dockerfile), f"{key}.dockerfile")
        if build.get("ssh"):
            self._refuse(f"{key}.ssh", "SSH agent forwarding is unsupported in v1")
        if build.get("network") == "host":
            self._refuse(f"{key}.network", "host networking cannot reproduce the laptop host")
        contexts = build.get("additional_contexts", {})
        if isinstance(contexts, list):
            contexts = dict(entry.split("=", 1) for entry in contexts)
        for context_name, value in contexts.items():
            if self._remote_context(value):
                # Compose service contexts and registry images stay engine-local.
                if not value.startswith(("service:", "docker-image://")):
                    self._refuse(f"{key}.additional_contexts.{context_name}", "remote build contexts are unsupported")
            else:
                self._local_path(value, f"{key}.additional_contexts.{context_name}")

    def validate(self, model: dict) -> None:
        if not isinstance(model, dict) or not isinstance(model.get("services"), dict) or not model["services"]:
            raise PodgroveError("services: Compose project has no active services; check compose.profiles")
        for kind in ("networks", "volumes", "configs", "secrets"):
            for name, definition in (model.get(kind) or {}).items():
                definition = definition or {}
                key = f"{kind}.{name}"
                if definition.get("external"):
                    self._refuse(f"{key}.external", "external host resources do not exist on an isolated engine")
                if kind in ("networks", "volumes"):
                    driver = definition.get("driver")
                    supported = (None, "bridge") if kind == "networks" else (None, "local")
                    if driver not in supported:
                        self._refuse(f"{key}.driver", "only the standard isolated Docker driver is supported")
                    if kind == "volumes" and definition.get("driver_opts"):
                        self._refuse(f"{key}.driver_opts", "host/device volume drivers cannot be reproduced")
                if kind in ("configs", "secrets") and "file" in definition:
                    path = self._check_sync_path(definition["file"], f"{key}.file")
                    if not path.is_file():
                        self._refuse(f"{key}.file", "expected a regular file")
        for name, service in model["services"].items():
            key = f"services.{name}"
            for field in (
                "devices", "device_cgroup_rules", "gpus", "external_links", "volumes_from", "use_api_socket",
                "provider", "models",
            ):
                if service.get(field):
                    self._refuse(f"{key}.{field}", "host-coupled resources are unsupported on the remote engine")
            for field in ("network_mode", "pid", "ipc", "uts", "cgroup"):
                value = service.get(field, "")
                if value == "host" or str(value).startswith("container:"):
                    self._refuse(f"{key}.{field}", "host or external-container namespaces are unsupported")
            if service.get("cgroup_parent"):
                self._refuse(f"{key}.cgroup_parent", "host cgroup paths are unsupported")
            devices = service.get("deploy", {}).get("resources", {}).get("reservations", {}).get("devices")
            if devices:
                self._refuse(f"{key}.deploy.resources.reservations.devices", "device reservations are unsupported")
            for index, volume in enumerate(service.get("volumes", [])):
                volume_key = f"{key}.volumes[{index}]"
                if not isinstance(volume, dict):
                    self._refuse(volume_key, "expected normalized Compose mount; run docker compose config")
                if volume.get("type") == "bind":
                    self._check_sync_path(volume.get("source", ""), f"{volume_key}.source")
                    propagation = volume.get("bind", {}).get("propagation", "rprivate")
                    if propagation != "rprivate":
                        self._refuse(f"{volume_key}.bind.propagation", "shared/slave host mount propagation is unsupported")
                elif volume.get("type") not in ("volume", "tmpfs", "image"):
                    self._refuse(f"{volume_key}.type", "unsupported remote mount type")
            for env in service.get("env_file", []):
                if isinstance(env, str):
                    env = {"path": env}
                path = self._local_path(env["path"], f"{key}.env_file", required=env.get("required", True))
                if path.exists() and not path.is_file():
                    self._refuse(f"{key}.env_file", "expected a regular file")
            if service.get("build"):
                self._validate_build(name, service["build"])
            for rule in service.get("develop", {}).get("watch", []):
                watch_key = f"{key}.develop.watch.path"
                self._check_sync_path(rule["path"], watch_key, mirror=False)
            for port in service.get("ports", []):
                if port.get("protocol", "tcp") != "tcp":
                    self._refuse(f"{key}.ports.protocol", "only TCP publishing can be port-forwarded by Kubernetes")
                if port.get("host_ip", "0.0.0.0") not in ("0.0.0.0", "127.0.0.1", ""):
                    self._refuse(f"{key}.ports.host_ip", "only IPv4 wildcard or loopback publishing is supported")
                if port.get("mode", "ingress") not in ("ingress", "host"):
                    self._refuse(f"{key}.ports.mode", "unsupported port publishing mode")
                published = str(port.get("published", "0"))
                if published == "2375":
                    self._refuse(f"{key}.ports.published", "2375 is reserved for the isolated Docker API")
                if "-" in published:
                    try:
                        start, end = (int(part) for part in published.split("-", 1))
                    except ValueError:
                        pass  # published_ports below gives the actionable schema error.
                    else:
                        if start <= 2375 <= end:
                            self._refuse(f"{key}.ports.published", "range includes 2375, reserved for the Docker API")
        self.published_ports(model)  # Check target and published ranges before mutation.
        self._validate_forwards(model)

    def _validate_forwards(self, model: dict) -> None:
        ports = self.published_ports(model)
        targets = self.config.forward
        if targets is None:
            targets = [{"service": port["service"], "port": port["target"]} for port in ports]
        for forward in targets:
            name, target = forward["service"], forward["port"]
            if name not in model["services"]:
                self._refuse("forward.service", f"service {name!r} is missing or not enabled by compose.profiles")
            matching = [port for port in ports if port["service"] == name and port["target"] == target]
            if len(matching) != 1:
                self._refuse("forward.port", f"{name}:{target} must have exactly one published TCP port")
            service = model["services"][name]
            replicas = service.get("scale", service.get("deploy", {}).get("replicas", 1))
            if replicas != 1:
                self._refuse(f"services.{name}.deploy.replicas", "forwarding a scaled service is ambiguous; use one replica")

    def sync_paths(self, model: dict) -> list[Path]:
        paths: set[Path] = set()
        for service in model.get("services", {}).values():
            for volume in service.get("volumes", []):
                if volume.get("type") == "bind":
                    paths.add(self._local_path(volume["source"], "volumes.source"))
        for kind in ("configs", "secrets"):
            for definition in (model.get(kind) or {}).values():
                if definition and "file" in definition:
                    paths.add(self._local_path(definition["file"], f"{kind}.file"))
        # Compose watch operates through CopyToContainer; these aren't daemon
        # host bind sources and do not need an additional remote mirror.
        ordered = sorted(paths, key=lambda path: (len(path.parts), str(path)))
        roots: list[Path] = []
        for path in ordered:
            if not any(path.is_relative_to(parent) for parent in roots):
                roots.append(path)
        return roots

    def published_ports(self, model: dict) -> list[dict]:
        ports = []
        for name, service in model.get("services", {}).items():
            for port in service.get("ports", []):
                key = f"services.{name}.ports"
                try:
                    target = int(port["target"])
                    published_value = str(port.get("published", "0"))
                    if "-" in published_value:
                        # Compose can allocate from a range, but one target has
                        # one runtime mapping; inspect resolves the actual port.
                        start, end = (int(part) for part in published_value.split("-", 1))
                        if not 1 <= start <= end <= 65535:
                            raise ValueError
                        published: int | str = published_value
                    else:
                        published = int(published_value)
                        if not 0 <= published <= 65535:
                            raise ValueError
                    if not 1 <= target <= 65535:
                        raise ValueError
                except (KeyError, TypeError, ValueError) as exc:
                    raise PodgroveError(f"{key}: invalid published or target port") from exc
                ports.append({"service": name, "target": target, "published": published,
                              "protocol": port.get("protocol", "tcp")})
        return ports

    def has_watch(self, model: dict) -> bool:
        return any(service.get("develop", {}).get("watch") for service in model.get("services", {}).values())
