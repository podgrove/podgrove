# Adopt a Compose project

Podgrove runs your existing Docker Compose stack on a separate Docker engine in Kubernetes. Adopting it means adding runtime settings in `podgrove.yml`; services, networks, volumes, healthchecks, dependencies and watch rules stay in Compose. There is no service-to-Deployment conversion.

Use this guide for human or agent-led adoption. Complete [installation and namespace setup](getting-started.md) first, then preserve the project’s actual invocation and test its application behavior.

## Agent setup checklist

1. [Install a pinned Podgrove release](getting-started.md#install) and save its executable as `PODGROVE_BIN`.
2. In the **application worktree**, create `podgrove.yml` with its approved cluster context, namespace, namespace mode, and StorageClass. Add the project's actual Compose invocation using [the mapping below](compose-migration.md#1-preserve-the-projects-compose-invocation); retain the existing Compose service definitions.
3. [Generate bootstrap manifests](getting-started.md#configure-the-target-and-prepare-access) from that application worktree: `"$PODGROVE_BIN" bootstrap --output /existing/parent/new-folder`. The folder is generated from YAML; there is no fixed `deploy/bootstrap` folder to apply.
4. Have the administrator ensure the printed target namespace already exists, review the generated folder, arrange developer authentication, and run its printed `kubectl --context ... --namespace ... apply -f ...` command on the same cluster. Shared mode needs this once per namespace; worktree mode needs it for **each worktree**. [Platform guide](../deploy/README.md).
5. From the application worktree, run `validate`, `up --dry-run --json`, `doctor`, then `up` using `"$PODGROVE_BIN"`. Run the application's own health checks and business tests against the printed localhost endpoints; a successful startup alone is not application acceptance.
6. Use `"$PODGROVE_BIN" web` for the read-only dashboard. When finished, `"$PODGROVE_BIN" down` deletes that environment and its PVC data while keeping namespace access ready for reuse.

Copy a starting configuration, replace its example values, and adjust Compose files to match the application:

| Example | Use |
| --- | --- |
| [Shared namespace](../examples/shared/podgrove.yml) | Several worktrees, each with its own engine and PVC, using one namespace installation. |
| [Namespace per worktree](../examples/worktree/podgrove.yml) | Each worktree gets a derived namespace and a separate bootstrap installation. |
| [Committed worktree configuration](../examples/portable/README.md) | Keep one relative config in Git and reuse it in linked worktrees. |
| [Dashboard only](../examples/dashboard/podgrove.yml) | Read an existing target without a Compose project. |
| [Network exclusions](../examples/network/podgrove.yml) | Add administrator-supplied infrastructure ranges to the worktree's egress exclusions. |
| [Custom resources](../examples/resources/podgrove.yml) and [Compose service](../examples/resources/compose.yml) | Set engine/initializer requests and limits, PVC capacity, and separate per-service Compose limits. |

## Adopt an existing Compose project

### 1. Preserve the project's Compose invocation

Inspect the project's own development/test instructions before adding Podgrove configuration. Record its real Compose files in overlay order, project name, enabled profiles, interpolation env file, Compose project directory, required source files, and health/test commands. Use development credentials. Do not invent an overlay, discard unsupported fields, or duplicate service definitions in `podgrove.yml` to make validation pass.

| Existing Compose input | Podgrove setting |
| --- | --- |
| Ordered `-f` / `--file` arguments | `compose.files`, in exactly the same order |
| Explicit `-p` / `--project-name` | Preserve through Compose's `COMPOSE_PROJECT_NAME`; Podgrove has no project-name flag |
| `--profile` selections | `compose.profiles` |
| Interpolation `--env-file` | `compose.env_file` |
| Compose `--project-directory` / relative-path base | `compose.project_directory`, inside the selected worktree |
| Service `environment`, `env_file`, build options, ports, volumes, healthchecks, dependencies, watch rules | Keep in the original Compose files |

Select the application worktree root containing all required local sources, then create `podgrove.yml` there. This example assumes the project actually uses a single `docker-compose.yml` at its root and the administrator has prepared the [approved target](getting-started.md#configure-the-target-and-prepare-access):

```yaml
version: 1
cluster:
  context: your-development-context
  namespace: your-development-namespace
  namespace_mode: shared
  storage_class: your-delete-storage-class
compose:
  files:
    - docker-compose.yml
  project_directory: .
size: medium
node_mode: shared
ttl: 8h
```

Replace the context and file selection with the project's actual values. If the existing invocation also uses `docker-compose.dev.yml`, profile `frontend`, and interpolation file `.env.development`, its **`compose` section** would instead be:

```yaml
compose:
  files: [docker-compose.yml, docker-compose.dev.yml]
  profiles: [frontend]
  env_file: .env.development
  project_directory: .
```

Only name files and profiles that exist. An explicit `compose.files` list or repeated CLI `-f` arguments is the complete ordered list: Podgrove adds no automatic override. If both are omitted, it discovers a standard Compose base and optional standard override within the worktree. Repeated `-f` replaces the YAML list. `compose.env_file` controls interpolation; a service's own `env_file` remains in Compose.

If the original command supplies a project name with `-p`, export that same `COMPOSE_PROJECT_NAME` consistently for validation, startup, and subsequent lifecycle commands. Otherwise retain the project's existing Compose `name:` or normal naming configuration. Podgrove does not choose a new Compose project name to isolate worktrees; separate Docker engines provide that isolation.

### 2. Check path and remote-host assumptions

The CLI's `--project-directory` selects where Podgrove starts looking; it defaults to the current directory. Inside Git, the nearest checkout’s top level determines the worktree identity, including when you run from a subdirectory. The nearest `podgrove.yml` upward within that checkout determines the configuration and allowed-source boundary. Outside Git, the selected directory is the identity boundary.

`compose.project_directory` chooses the **Compose path-resolution base inside the configuration boundary**, defaulting to `.`. `compose.files` and `compose.env_file` are relative to that configuration directory. Paths inside Compose use the Compose base. A nested `-f` filename alone does not change that base. An explicit `--config` resolves inside the CLI-selected directory and retains that directory as the allowed-source boundary.

For an independent test stack under `tests/`, use this `compose` section while keeping the worktree root at the application root:

```yaml
compose:
  files: [tests/docker-compose.test.yml]
  project_directory: tests
```

A bind such as `../services:/app/services` then stays within the worktree. The [configuration reference](configuration.md#two-directory-settings) explains both directory settings.

Check these before startup:

- Required build, bind, config, and secret sources must exist within the configuration and allowed-source boundary. Synchronized sources cannot contain symlinks or special files; external paths and `.git` sources are refused. Broad binds can include large generated trees, virtual environments, and symlinked dependencies: Podgrove does not silently ignore them.
- The mirror preserves numeric UID/GID and file modes. A local owner-only file may be unreadable to the application's non-root Linux user; prepare compatible development sources rather than assuming Docker Desktop's sharing behavior.
- Existing laptop images and named-volume databases are not migrated. The remote engine builds/pulls images and starts with fresh named volumes; those volumes persist on its PVC until cleanup. Seed or import test data through the project's normal workflow.
- Laptop services, host devices, private-network dependencies, and external Docker resources do not move with the worktree. Default engine egress allows DNS and public IPv4 HTTP(S); other destinations require reviewed platform changes. Unsupported Compose features fail explicitly; consult [known limits](known-limits.md#refused-compose-features).

### 3. Validate, inspect, then start

With the config saved and the cluster prerequisites satisfied:

```sh
cd /path/to/application-worktree
"$PODGROVE_BIN" validate
"$PODGROVE_BIN" up --dry-run --json
"$PODGROVE_BIN" doctor
```

`validate` checks configuration, paths, the normalized Compose model, and forwarding choices locally using Docker Compose; it needs no daemon or cluster connection. `validate` and `up --dry-run` still require an explicit namespace, but need no context for their local checks. `up --dry-run` renders the Kubernetes manifests without contacting the cluster; run `validate` as well to check local port allocation. `doctor` checks namespace-scoped access and the provisioning marker, then dry-runs a new engine Pod against admission or validates an existing compatible engine. It does not provision a Pod, read node inventory, or validate StorageClass/reclaim settings. The existing namespace and reviewed bootstrap must be prepared by its administrator before startup. None of these checks establishes that the application works.

Start the environment after those checks succeed:

```sh
"$PODGROVE_BIN" up
"$PODGROVE_BIN" status --json
```

`up` checks owned PVC compatibility and the administrator-approved class selection before resource creation, prepares the engine and mirror, runs Compose, and waits for readiness and tunnels. It returns while a background session keeps file sync, Compose watch, the Docker API connection, and application tunnels running. A running service without a healthcheck counts as ready; a successful job required through `service_completed_successfully` is also accepted. Use the project's business tests to verify more than readiness.

The engine's PVC request comes from `storage.size` in `podgrove.yml`, defaulting to `20Gi`; `up --storage` overrides it for that invocation. Save a custom capacity in YAML so normal launches, refreshes and reconnects use it consistently. `cluster.storage_class` supplies the startup class; `up --storage-class` overrides it. A new PVC requires an explicit administrator-approved class. Reconnect can reuse the class already recorded on its owned PVC. Podgrove never discovers default classes or reads StorageClass/PV metadata. Bootstrap needs no class and grants no cluster storage access. Resource or PVC configuration changes do not resize an existing environment; see [custom resources](operations.md#custom-resources-and-storage).

### 4. Verify the adopted project

Before declaring adoption complete:

1. Check `status` and the application's declared readiness, including any initialization jobs.
2. Run the project's HTTP, WebSocket, database, and business tests against the **printed laptop endpoints**. Do not reuse the old local Compose port numbers blindly.
3. Make a controlled source edit covered by a bind mount or declared watch rule and verify the remote application observes it. Application reload behavior remains the project's responsibility. Revert the edit afterward.
4. Write disposable test data, run `up --refresh`, and verify it survives on named volumes. Refresh re-runs the build/start workflow while retaining the engine/PVC; it is not a request to erase data.
5. Save needed diagnostics, run `down` when finished, and verify the owned resources are removed. Record the actual commands, results, and anything blocked. A dry-run, dashboard screenshot, or fixture pass alone is not end-to-end application acceptance.
