"""The public CLI. Cluster identity is always explicit; Compose stays authoritative."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import __version__, state
from .compose import Compose
from .config import default_tainted_nodes, load_cluster, load_config, storage_class_name
from .errors import PodgroveError
from .forward import port_plan
from .kube import Kube, context_name, manifests, namespace_name, resolve_namespace
from .process import docker_environment
from .reaper import mr_endpoint, reap
from .session_status import observed
from .resources import engine_resources, initializer_resources, quantity_text


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="podgrove", description="Run unchanged Docker Compose worktrees on Kubernetes")
    p.add_argument("--version", action="version", version=__version__)
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("up", "status", "env", "logs", "exec", "down", "validate", "doctor", "reap"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--context", help="Override cluster.context in podgrove.yml")
        cmd.add_argument("--namespace", help="Explicit target namespace; overrides cluster.namespace")
        cmd.add_argument("--namespace-mode", choices=("shared", "worktree"),
                         help="Override cluster.namespace_mode; worktree derives a namespace from the configured base")
        cmd.add_argument("--project-directory", type=Path, default=Path("."))
        cmd.add_argument("--config", type=Path)
        cmd.add_argument("-f", "--file", action="append", dest="files")
        cmd.add_argument("--json", action="store_true")
        if name in ("up", "doctor"):
            cmd.add_argument("--node-mode", choices=("shared", "tainted"),
                             help="Node placement mode; overrides node_mode in podgrove.yml (default: shared)")
        if name == "up":
            cmd.add_argument("--size", choices=("small", "medium", "large"),
                             help="Use a preset instead of YAML size/resources")
            cmd.add_argument("--storage-class", help="Override cluster.storage_class")
            cmd.add_argument("--storage", help="Override storage.size in podgrove.yml (default: 20Gi)")
            cmd.add_argument("--timeout", type=int, default=600)
            cmd.add_argument("--mr-url", default="")
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--refresh", action="store_true", help="Re-run Compose build/up while retaining this environment's volumes")
        if name in ("logs", "exec"):
            cmd.add_argument("service")
        if name == "logs":
            cmd.add_argument("--follow", action="store_true")
            cmd.add_argument("--tail", type=int, default=100)
        if name == "status":
            cmd.add_argument("--all", action="store_true", help="List local environments for the explicit context")
        if name == "exec":
            cmd.add_argument("args", nargs=argparse.REMAINDER)
        if name == "reap":
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--watch", action="store_true")
            cmd.add_argument("--interval", type=int, default=60)
            scope = cmd.add_mutually_exclusive_group()
            scope.add_argument("--environment", help="Limit cleanup to one environment identity")
            scope.add_argument("--all", action="store_true", help="Consider every Podgrove environment in the selected namespace")
    web = commands.add_parser("web", help="Open a read-only local environment dashboard")
    web.add_argument("--context", help="Override cluster.context in podgrove.yml")
    web.add_argument("--namespace", help="Override cluster.namespace; filter locally recorded environments")
    web.add_argument("--namespace-mode", choices=("shared", "worktree"), help="Override cluster.namespace_mode")
    web.add_argument("--project-directory", type=Path, default=Path("."), help="Directory containing podgrove.yml")
    web.add_argument("--config", type=Path, help="Alternate Podgrove config inside the project directory")
    web.add_argument("--port", type=int, default=0, help="Loopback port; zero selects an available port")
    web.add_argument("--no-open", action="store_true", help="Print the URL without opening a browser")
    bootstrap = commands.add_parser("bootstrap", help="Render namespace access and safeguards for administrator review")
    bootstrap.add_argument("--context", help="Override cluster.context in podgrove.yml")
    bootstrap.add_argument("--namespace", help="Override the namespace; required in config or as a flag")
    bootstrap.add_argument("--namespace-mode", choices=("shared", "worktree"), help="Override cluster.namespace_mode")
    bootstrap.add_argument("--storage-class", help="Validate an intended StorageClass name; bootstrap grants no cluster access")
    bootstrap.add_argument("--project-directory", type=Path, default=Path("."))
    bootstrap.add_argument("--config", type=Path)
    bootstrap.add_argument("--output", type=Path, required=True, help="New directory for the generated Kubernetes YAML")
    bootstrap.add_argument("--developer-group", default="podgrove-developers", help="Kubernetes group to bind")
    internal = commands.add_parser("_serve", help=argparse.SUPPRESS)
    internal.add_argument("state", type=Path)
    return p


def print_state(data: dict, as_json=False) -> None:
    public = {k: v for k, v in data.items() if k not in ("token", "socket", "docker_host")}
    if as_json:
        print(json.dumps(public, indent=2))
        return
    label = data.get("identity", "unknown")
    root = data.get("root", "")
    print(f"{data['namespace']} / {label}: {data.get('status', 'unknown')}" + (f"  {root}" if root else ""))
    for port in data.get("ports", []):
        readiness = port.get("status", "unknown")
        print(f"  {port['service']}:{port['target']}  {port['url']}  [{readiness}]")
    for row in data.get("services", []):
        print(f"  {row.get('Service', '?')}: {row.get('State', '?')} {row.get('Health', '')}".rstrip())
    if data.get("error"):
        print(f"  {data['error']}")
    if data.get("session_log"):
        print(f"  Session log: {data['session_log']}")


def _set_target(args, target: dict, *, required: bool = True) -> None:
    """Resolve each field: command line, project YAML, then context environment fallback."""
    args._explicit_namespace = args.namespace
    context = args.context if args.context is not None else target.get("context")
    if context is None:
        context = os.environ.get("PODGROVE_CONTEXT")
    namespace = args.namespace if args.namespace is not None else target.get("namespace")
    if namespace is None:
        raise PodgroveError("No namespace provided. Set cluster.namespace in podgrove.yml or provide --namespace; "
                            "Podgrove never selects a default namespace implicitly.")
    args.context = context_name(context) if required or context is not None else None
    args.namespace_base = namespace_name(namespace)
    # A namespace flag is an exact recovery/override target unless a mode flag
    # explicitly asks to derive a worktree namespace from it.
    args.namespace_mode = (args.namespace_mode or
                           ("shared" if args._explicit_namespace is not None else target.get("namespace_mode", "shared")))
    if args.namespace_mode == "shared":
        args.namespace = args.namespace_base
    else:
        try:
            root = args.project_directory.expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise PodgroveError("Worktree namespace mode requires a resolvable --project-directory") from exc
        args.namespace = resolve_namespace(args.namespace_base, args.namespace_mode, state.identity(root))


def _resolve_target(args, root: Path | None) -> None:
    target = {}
    # Explicit flags keep cleanup and global inventory usable even when the
    # project's configuration is broken. An explicit --config is always read.
    if args.config is not None or args.context is None or args.namespace is None:
        if root is None:
            try:
                root = args.project_directory.expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                if args.config is not None or args.project_directory != Path("."):
                    raise PodgroveError("Cannot resolve the configuration project directory") from exc
                if args.context is None and not os.environ.get("PODGROVE_CONTEXT"):
                    raise PodgroveError("Set cluster.context in podgrove.yml or provide --context from a usable directory") from exc
        if root is not None:
            # A removed worktree must not prevent cleanup with an explicit
            # context; there is no file to read in this case.
            if root.is_dir() or args.config is not None:
                target = load_cluster(root, args.config)
            elif args.context is None and not os.environ.get("PODGROVE_CONTEXT"):
                raise PodgroveError(f"Project directory does not exist: {root}")
    _set_target(args, target)


def up(args, root: Path) -> int:
    from .runtime import control, is_running, spawn
    _set_target(args, load_cluster(root, args.config), required=not args.dry_run)
    config = load_config(root, args.config, args.files)
    node_mode = args.node_mode or config.node_mode
    compose = Compose(config)
    model = compose.model()
    compose.validate(model)
    fingerprint_inputs = {"model": model, "forward": config.forward,
                          "ttl": config.ttl_seconds, "network": config.network}
    # An absent new optional setting must not refresh an unchanged legacy stack.
    if config.sync_exclude:
        fingerprint_inputs["sync_exclude"] = config.sync_exclude
    fingerprint = hashlib.sha256(json.dumps(fingerprint_inputs, sort_keys=True).encode()).hexdigest()
    ident = state.identity(root)
    # Validate all constraints including forwarding before any Kubernetes mutation.
    if args.mr_url:
        mr_endpoint(args.mr_url)
    if args.timeout < 1:
        raise PodgroveError("--timeout must be positive")
    namespace = args.namespace
    storage_class = (storage_class_name(args.storage_class) if args.storage_class is not None
                     else config.storage_class)
    budget = engine_resources(args.size or config.size, config.resources if args.size is None else None)
    init_budget = initializer_resources(config.init_resources)
    storage_size = quantity_text(args.storage if args.storage is not None else config.storage_size, "storage.size")
    resources = manifests(namespace, ident, root, args.size or config.size, config.ttl_seconds,
                          storage_class=storage_class,
                          storage=storage_size, mr_url=args.mr_url, namespace_mode=args.namespace_mode,
                          node_mode=node_mode, tainted_nodes=config.tainted_nodes, network=config.network,
                          resources=budget, init_resources=init_budget)
    if args.dry_run:
        print(json.dumps({"identity": ident, "namespace": namespace, "namespace_mode": args.namespace_mode,
                          "resources": resources}, indent=2))
        return 0
    kube = Kube(args.context, namespace, namespace_mode=args.namespace_mode)
    path = state.state_path(root, args.context)
    with state.lock(path):
        if path.exists():
            old = state.read(path)
            state.validate_binding(old, root, args.context)
            args.mr_url = args.mr_url or old.get("mr_url", "")
            if args.mr_url:
                mr_endpoint(args.mr_url)
            for resource in resources:
                if resource["kind"] == "ConfigMap":
                    resource["data"]["mr_url"] = args.mr_url
            if old["namespace"] != namespace:
                raise PodgroveError("Existing environment uses another namespace; down it before changing namespaces")
            if state.namespace_mode(old) != args.namespace_mode:
                raise PodgroveError("Existing environment uses another namespace mode; down it before changing modes")
            previous_node_mode = old.get("node_mode", "tainted")
            if previous_node_mode == "dedicated":
                previous_node_mode = "tainted"
            previous_tainted_nodes = old.get("tainted_nodes", default_tainted_nodes())
            placement_changed = node_mode == "tainted" and previous_tainted_nodes != config.tainted_nodes
            if previous_node_mode != node_mode or placement_changed:
                raise PodgroveError(
                    f"Existing environment uses node_mode={previous_node_mode} with its recorded node placement; "
                    f"requested node_mode={node_mode} has different placement. "
                    "Run podgrove down, then up to recreate it with the new placement; down removes its stored data. "
                    "--refresh preserves scheduling and cannot change node placement."
                )
            recent_start = isinstance(old.get("created_at"), (float, int)) and time.time() - old["created_at"] < args.timeout * 3 + 120
            if old.get("status") == "starting" and (recent_start or (old.get("socket") and Path(old["socket"]).exists())):
                raise PodgroveError("This environment is still starting in the background; inspect its session log or run down before retrying")
            if is_running(old):
                # Check the declared engine/PVC budget even on the fast path.
                # An incompatible refresh must not stop a working supervisor.
                kube.check_engine_settings(resources)
                kube.check_storage(resources)
                # Even an unchanged running stack must not retain a missing or
                # weakened policy. Reconcile only this owned engine's policy.
                kube.reconcile_network_policy(resources, ident)
                ping = control(old, "ping") if "forward_status" in old else None
                if ping is not None and (not isinstance(ping, dict) or ping.get("ok") is not True):
                    raise PodgroveError("Session is stopping or rejected the control request; retry up after it stops")
                health = observed(old, connected=True, ping=ping)
                changed = (old.get("compose_fingerprint") and old["compose_fingerprint"] != fingerprint) or args.mr_url != old.get("mr_url", "")
                if not args.refresh and not changed and health["forward_status"]["state"] != "disconnected":
                    print_state(health, args.json)
                    return 0 if health["status"] == "ready" else 1
                kube.preflight(node_mode=node_mode, tainted_nodes=config.tainted_nodes)
                kube.check_storage(resources)
                control(old, "stop")
                deadline = time.monotonic() + 30
                while Path(old["socket"]).exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                if Path(old["socket"]).exists():
                    raise PodgroveError("Existing session is still stopping; retry up after it finishes")
        port_plan(compose.published_ports(model), config.forward, ident)
        kube.preflight(node_mode=node_mode, tainted_nodes=config.tainted_nodes)
        kube.check_storage(resources)
        kube.ensure_namespace(ident, resources[0]["metadata"]["labels"])
        effective_storage_class = next(item["spec"].get("storageClassName") for item in resources
                                       if item["kind"] == "PersistentVolumeClaim")
        # Record intent before creation so partial failures remain discoverable by down.
        token = uuid.uuid4().hex
        data = {"identity": ident, "root": str(root), "context": args.context, "namespace": namespace,
                "namespace_mode": args.namespace_mode,
                "config_path": str((root / (args.config or Path("podgrove.yml"))).resolve())
                    if args.config is not None or (root / "podgrove.yml").exists() else None,
                "files": [str(path) for path in config.files],
                "compose_project": model.get("name"), "compose_services": sorted(model["services"]),
                "timeout": args.timeout, "status": "starting", "token": token,
                "socket": str(Path(tempfile.gettempdir()) / f"podgrove-{os.getuid()}-{token[:16]}.sock"),
                "ttl_seconds": config.ttl_seconds, "mr_url": args.mr_url,
                "node_mode": node_mode,
                "tainted_nodes": config.tainted_nodes,
                "network": config.network,
                "resources": budget, "init_resources": init_budget,
                "storage": {"size": storage_size, "storage_class": effective_storage_class},
                "compose_fingerprint": fingerprint,
                "created_at": time.time()}
        state.write(path, data)
        try:
            kube.create_environment(resources, ident)
            spawn(path)
        except Exception as exc:
            data.update({"status": "error", "error": str(exc)})
            state.write(path, data)
            raise
        # Readiness includes initial sync, build, jobs/healthchecks, and every tunnel.
        deadline = time.monotonic() + args.timeout * 3 + 120
        while time.monotonic() < deadline:
            data = state.read(path)
            if data["status"] == "ready":
                print_state(data, args.json)
                return 0
            if data["status"] == "error":
                raise PodgroveError(f"{data['error']}\nSession log: {path.with_suffix('.log')}\n"
                                    "Resources retained for diagnosis; podgrove down cleans them up.")
            if data["status"] in ("disconnected", "reaped"):
                raise PodgroveError("Startup was stopped; run up to reconnect or down to remove the environment")
            time.sleep(0.25)
        raise PodgroveError("Startup timed out; inspect podgrove status and the session log, or run podgrove down")


def execute(args) -> int:
    if args.command == "bootstrap":
        from .bootstrap import generate_bootstrap
        root = args.project_directory.expanduser().resolve()
        cluster = load_cluster(root, args.config)
        _set_target(args, cluster)
        storage_class = args.storage_class if args.storage_class is not None else cluster.get("storage_class")
        paths = generate_bootstrap(args.output, namespace=args.namespace, storage_class=storage_class,
                                   developer_group=args.developer_group,
                                   namespace_mode=args.namespace_mode,
                                   identity=state.identity(root) if args.namespace_mode == "worktree" else None)
        print(f"Generated {len(paths)} manifests for context {args.context}, namespace {args.namespace} "
              f"({args.namespace_mode}) in {args.output.absolute()}")
        print("The namespace must already exist. Offline generation cannot inspect its other workloads.")
        print("Review the namespace-scoped manifests, then apply with an authorized namespace administrator:")
        print(shlex.join(["kubectl", "--context", args.context, "--namespace", args.namespace,
                          "apply", "-f", str(args.output.absolute())]))
        return 0
    if args.command == "web":
        from .web import serve
        _resolve_target(args, None)
        return serve(args.context, port=args.port, namespace=args.namespace, open_browser=not args.no_open)
    if args.command == "_serve":
        from .runtime import serve
        return serve(args.state)
    root = None if args.command == "status" and args.all else args.project_directory.expanduser().resolve()
    if args.command == "validate":
        _set_target(args, load_cluster(root, args.config), required=False)
        config = load_config(root, args.config, args.files)
        compose = Compose(config)
        model = compose.model()
        compose.validate(model)
        port_plan(compose.published_ports(model), config.forward, state.identity(root))
        print(json.dumps({"valid": True, "services": sorted(model["services"]), "sync_paths": [str(p) for p in compose.sync_paths(model)]}, indent=2))
        return 0
    if args.command == "up":
        return up(args, root)
    _resolve_target(args, root)
    if args.command == "status" and args.all:
        from .runtime import is_running, control
        entries = state.local_records(args.context, args.namespace)
        rows = []
        for entry in entries:
            if "error" in entry:
                rows.append({"identity": entry["path"].stem.split("-")[0], "status": "error",
                             "error": entry["error"], "state_file": str(entry["path"])})
                continue
            data = entry["data"]
            connected = is_running(data)
            try:
                ping = control(data, "ping") if connected else None
                if connected:
                    connected = isinstance(ping, dict) and ping.get("ok") is True
            except PodgroveError:
                connected, ping = False, None
            data = observed(data, connected=connected, ping=ping)
            public = {key: data[key] for key in ("identity", "root", "context", "namespace", "status", "created_at",
                                                 "last_activity", "ttl_seconds", "node_mode", "namespace_mode", "ports", "error",
                                                 "forward_status", "sync_status") if key in data}
            rows.append(public)
        if args.json:
            print(json.dumps({"environments": rows}, indent=2))
        elif not rows:
            print(f"No local environments for context {args.context}")
        else:
            for row in rows:
                print(f"{row['identity']} {row.get('namespace', '?')}: {row['status']} {row.get('root', '')}".rstrip())
                if row.get("error"):
                    print(f"  {row['error']}")
        return 1 if any("error" in entry for entry in entries) else 0
    ident = state.identity(root)
    kube = Kube(args.context, args.namespace, namespace_mode=args.namespace_mode)
    if args.command == "doctor":
        node_mode = "shared"
        tainted_nodes = default_tainted_nodes()
        size, ttl = "medium", 8 * 3600
        budget, init_budget, storage_size, storage_class = None, None, "20Gi", None
        if args.config is not None or args.files is not None or (root / "podgrove.yml").exists():
            config = load_config(root, args.config, args.files, require_compose=False)
            node_mode, tainted_nodes = config.node_mode, config.tainted_nodes
            size, ttl = config.size, config.ttl_seconds
            budget, init_budget = config.resources, config.init_resources
            storage_size, storage_class = config.storage_size, config.storage_class
        node_mode = args.node_mode or node_mode
        kube.preflight(node_mode=node_mode, tainted_nodes=tainted_nodes)
        namespace = args.namespace
        kube.ensure_namespace(ident)
        kube.check_admission(manifests(namespace, ident, root, size, ttl,
                                       node_mode=node_mode, tainted_nodes=tainted_nodes,
                                       namespace_mode=args.namespace_mode, resources=budget, init_resources=init_budget,
                                       storage=storage_size, storage_class=storage_class))
        print("Namespace permissions, bootstrap marker and engine admission checks passed")
        print(f"{node_mode.capitalize()} scheduling is expressed by the Pod spec; node inventory is not read.")
        print("StorageClass reclaim policy and backing-volume deletion require administrator verification.")
        return 0
    if args.command == "reap":
        if not args.namespace:
            raise PodgroveError("reap requires cluster.namespace in podgrove.yml or --namespace; it never scans arbitrary namespaces")
        if args.interval < 1:
            raise PodgroveError("--interval must be positive")
        while True:
            results = reap(kube, args.dry_run, identity=None if args.all else (args.environment or ident))
            print(json.dumps(results, indent=2), flush=True)
            if not args.watch:
                return 1 if any("error" in item for item in results) else 0
            time.sleep(args.interval)
    path = state.state_path(root, args.context)
    if args.command == "down":
        from .runtime import stop_session
        with state.lock(path):
            if path.exists():
                data = state.read(path)
                state.validate_binding(data, root, args.context)
                if args._explicit_namespace and args.namespace != data["namespace"]:
                    raise PodgroveError("--namespace differs from the recorded environment")
                kube = Kube(args.context, data["namespace"], namespace_mode=state.namespace_mode(data))
                stop_session(data)
            else:
                # Never infer a cleanup namespace without explicit config/flags.
                # A valid owned lease may recover legacy namespace lifecycle.
                recovered_mode = kube.lease_mode(ident)
                kube = Kube(args.context, args.namespace, namespace_mode=recovered_mode)
                data = {"identity": ident, "root": str(root), "context": args.context,
                        "namespace": kube.namespace, "namespace_mode": recovered_mode, "status": "cleanup_pending"}
                state.write(path, data)
            kube.destroy(data["identity"])
            state.cleanup(path, data)
            print(f"Removed environment {data['identity']}; namespace and bootstrap retained")
            return 0
    data = state.read(path)
    state.validate_binding(data, root, args.context)
    if args._explicit_namespace and args.namespace != data["namespace"]:
        raise PodgroveError("--namespace differs from the recorded environment")
    kube = Kube(args.context, data["namespace"], namespace_mode=state.namespace_mode(data))
    from .runtime import control, is_running, readiness, service_status
    connected = is_running(data)
    # The control server can answer while provisioning/building, before the
    # Docker endpoint is published. A startup failure can briefly do so too.
    if data.get("status") == "starting" or (connected and not data.get("docker_host")):
        starting = data.get("status") == "starting"
        data["session_log"] = str(path.with_suffix(".log"))
        if not starting and data.get("status") != "error":
            data.update(status="error", error="Session Docker endpoint is not available")
        if args.command == "status":
            print_state(data, args.json)
            return 1
        detail = "Environment is still starting" if starting else "Session Docker endpoint is not available"
        raise PodgroveError(f"{detail}; inspect the session log at {data['session_log']} and run podgrove status")
    try:
        ping = control(data, "ping") if connected else None
        if connected:
            connected = isinstance(ping, dict) and ping.get("ok") is True
    except PodgroveError:
        connected, ping = False, None
    data = observed(data, connected=connected, ping=ping)
    if not connected:
        data["status"] = "disconnected" if data.get("status") != "error" else "error"
        if args.command == "status":
            print_state(data, args.json)
            return 1
        if args.command != "logs":
            raise PodgroveError("Session disconnected; run podgrove up to reconnect")
    if args.command == "env":
        if data["forward_status"]["state"] not in ("ready", "disabled"):
            raise PodgroveError("Local endpoints are not ready; inspect status or run up to reconnect")
        values = endpoint_environment(data["ports"])
        if args.json:
            print(json.dumps(values, indent=2))
        else:
            for key, value in values.items():
                print(f"export {key}={shlex.quote(value)}")
        return 0
    if connected:
        control(data, "touch")
    elif args.command == "logs" and data.get("compose_project") and data.get("compose_services"):
        return retained_logs(data, kube, args)
    config = load_config(root, Path(data["config_path"]) if data.get("config_path") else None, data.get("files"))
    compose = Compose(config)
    if args.command == "status":
        env = docker_environment(data["docker_host"])
        data["services"] = service_status(compose, env)
        ready, problems = readiness(compose.model(), data["services"])
        data["status"] = "ready" if ready else "unhealthy"
        data = observed(data, connected=True)
        data["problems"] = problems
        print_state(data, args.json)
        return 0 if ready and data["status"] == "ready" else 1
    model = compose.model()
    if args.service not in model["services"]:
        raise PodgroveError(f"Unknown Compose service: {args.service}")
    if args.command == "logs":
        if args.tail < 0:
            raise PodgroveError("--tail must be zero or greater")
        command = compose.command("logs", "--tail", str(args.tail), *( ["--follow"] if args.follow else []), args.service)
    else:
        cmd_args = args.args[1:] if args.args and args.args[0] == "--" else args.args
        if not cmd_args:
            raise PodgroveError("exec requires a command after --")
        command = compose.command("exec", *([] if sys.stdin.isatty() else ["-T"]), args.service, *cmd_args)
    with ExitStack() as cleanup:
        host = data.get("docker_host")
        if not connected:
            from .docker_tunnel import DockerTunnel
            tunnel = DockerTunnel(kube, ident, 0)
            cleanup.callback(tunnel.close)
            tunnel.start()
            host = f"tcp://127.0.0.1:{tunnel.port}"
        return subprocess.call(command, env=docker_environment(host), cwd=root)


def endpoint_environment(ports: list[dict]) -> dict[str, str]:
    import re
    values = {}
    for port in ports:
        if (not isinstance(port, dict) or not isinstance(port.get("service"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", port["service"])
                or any(type(port.get(key)) is not int or not 1 <= port[key] <= 65535 for key in ("target", "local"))):
            raise PodgroveError("Recorded endpoint metadata is invalid; inspect status and reconnect")
        service = re.sub(r"[^A-Z0-9]", "_", port["service"].upper())
        prefix = f"PODGROVE_{service}_{port['target']}"
        if prefix + "_HOST" in values:
            raise PodgroveError("Endpoint environment names collide; use status --json to select endpoints explicitly")
        values.update({prefix + "_HOST": "127.0.0.1", prefix + "_PORT": str(port["local"]),
                       prefix + "_URL": f"http://127.0.0.1:{port['local']}"})
    return values


def retained_logs(data: dict, kube: Kube, args) -> int:
    """Read original project containers even if its source config no longer parses."""
    import re
    from .docker_tunnel import DockerTunnel
    project, services = data["compose_project"], data["compose_services"]
    if (not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project)
            or not isinstance(services, list) or not services or any(
                not isinstance(service, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", service)
                for service in services)):
        raise PodgroveError("Recorded Compose project metadata is invalid")
    if args.service not in services:
        raise PodgroveError(f"Unknown Compose service: {args.service}")
    if args.tail < 0:
        raise PodgroveError("--tail must be zero or greater")
    with ExitStack() as cleanup:
        tunnel = DockerTunnel(kube, data["identity"], 0)
        cleanup.callback(tunnel.close)
        tunnel.start()
        # Compose logs selects existing containers by project/service labels.
        # This diagnostic-only model opens no project files and is never deployed.
        model = cleanup.enter_context(tempfile.NamedTemporaryFile(mode="w+", suffix=".json"))
        json.dump({"services": {service: {"image": "scratch"} for service in services}}, model)
        model.flush()
        command = ["docker", "compose", "--ansi", "never", "--project-name", project,
                   "--file", model.name, "logs", "--tail", str(args.tail),
                   *(["--follow"] if args.follow else []), args.service]
        return subprocess.call(command, env=docker_environment(f"tcp://127.0.0.1:{tunnel.port}"),
                               cwd=tempfile.gettempdir())


def main() -> int:
    try:
        return execute(parser().parse_args())
    except PodgroveError as exc:
        print(f"podgrove: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("podgrove: interrupted; an existing environment can be inspected with status or removed with down", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
