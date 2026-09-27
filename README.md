<p align="center"><img src="docs/assets/logo.svg" width="64" height="64" alt="Podgrove pods logo"></p>

# Podgrove

Run an existing Docker Compose worktree on its own Docker engine inside Kubernetes. One `podgrove up` prepares persistent storage, copies bind-mount sources before services start, runs the original Compose files, and prints localhost endpoints. A background session mirrors bind-source changes and runs declared Compose watch rules.

**Adopting Compose means adding runtime settings in `podgrove.yml`.** Services, networks, volumes, healthchecks, and dependencies stay in the original Compose files; Podgrove does not translate them into Kubernetes Deployments. Each worktree gets a separate engine and PVC, including when several worktrees share a namespace.

The read-only browser dashboard includes light/dark themes, a collapsible sidebar, live service and engine logs with fullscreen, branch/worktree identity, PVC allocation, an Endpoints tab, and Configuration tabs. Shared Linux EC2 placement is the default; a tainted node pool is optional.

For an agent adopting a project, follow the installation, cluster prerequisites, Compose mapping, and first-run verification below. The [configuration reference](docs/configuration.md), [schema](schema/podgrove-v1.schema.json), and [known limits](docs/known-limits.md) describe the supported surface. The [verification guide](docs/verification.md) explains the checks and what they establish.

## Agent setup checklist

1. [Install a pinned Podgrove release](#install) and save its executable as `PODGROVE_BIN`.
2. In the **application worktree**, create `podgrove.yml` with its approved cluster context, namespace, namespace mode, and StorageClass. Add the project's actual Compose invocation using [the mapping below](#1-preserve-the-projects-compose-invocation); retain the existing Compose service definitions.
3. [Generate bootstrap manifests](#configure-the-target-and-prepare-access) from that application worktree: `"$PODGROVE_BIN" bootstrap --output /existing/parent/new-folder`. The folder is generated from YAML; there is no fixed `deploy/bootstrap` folder to apply.
4. Have the administrator ensure the printed target namespace already exists, review the generated folder, arrange developer authentication, and run its printed `kubectl --context ... --namespace ... apply -f ...` command on the same cluster. Shared mode needs this once per namespace; worktree mode needs it for **each worktree**. [Platform guide](deploy/README.md).
5. From the application worktree, run `validate`, `up --dry-run --json`, `doctor`, then `up` using `"$PODGROVE_BIN"`. Run the application's own health checks and business tests against the printed localhost endpoints; a successful startup alone is not application acceptance.
6. Use `"$PODGROVE_BIN" web` for the read-only dashboard. When finished, `"$PODGROVE_BIN" down` deletes that environment and its PVC data while keeping namespace access ready for reuse.

Copy a starting configuration, replace its example values, and adjust Compose files to match the application:

| Example | Use |
| --- | --- |
| [Shared namespace](examples/shared/podgrove.yml) | Several worktrees, each with its own engine and PVC, using one namespace installation. |
| [Namespace per worktree](examples/worktree/podgrove.yml) | Each worktree gets a derived namespace and a separate bootstrap installation. |
| [Committed worktree configuration](examples/portable/README.md) | Keep one relative config in Git and reuse it in linked worktrees. |
| [Dashboard only](examples/dashboard/podgrove.yml) | Read an existing target without a Compose project. |
| [Network exclusions](examples/network/podgrove.yml) | Add administrator-supplied infrastructure ranges to the worktree's egress exclusions. |
| [Custom resources](examples/resources/podgrove.yml) and [Compose service](examples/resources/compose.yml) | Set engine/initializer requests and limits, PVC capacity, and separate per-service Compose limits. |

## Install

Requirements: macOS or Linux, Python 3.11 or newer, `uv` or pip, the Docker CLI with a recent Compose plugin, and `kubectl` with working authentication for the approved cluster. Compose must support `config --format json --no-env-resolution`, `watch --no-up` when watch rules are used, and the features present in your Compose files. Normal remote use needs no local Docker daemon. This repository's opt-in Docker integration tests do need one; Docker Desktop is not required.

### Install a verified release

For shared automation, install a published release into its own versioned environment.
The installer requires Python 3.11+, `uv`, and a complete release bundle; these download
commands also use the GitHub CLI. Choose a version from [Releases](https://github.com/podgrove/podgrove/releases).
From a checkout of that tag, download its five assets into a new directory:

```sh
git clone --branch v0.2.2 https://github.com/podgrove/podgrove.git
cd podgrove
PODGROVE_RELEASE_DIR="$(mktemp -d)"
gh release download v0.2.2 --repo podgrove/podgrove --dir "$PODGROVE_RELEASE_DIR"
PODGROVE_RELEASE_SHA="$(git rev-parse HEAD)"
python3 scripts/install_release.py --dist "$PODGROVE_RELEASE_DIR" \
  --tag v0.2.2 --source-sha "$PODGROVE_RELEASE_SHA"
export PODGROVE_BIN="$HOME/.local/share/podgrove/current/bin/podgrove"
"$PODGROVE_BIN" --version
```

The installer verifies asset hashes and the expected tag/commit, uses the release's
locked dependency hashes, checks the installed CLI and dashboard assets, and renders
bootstrap manifests offline. It creates `~/.local/share/podgrove/<version>-<commit>/venv`
and an `installation.json` receipt, then atomically switches `current` after every
check passes. The installation never reads kubeconfig or contacts a cluster.
Checksums establish integrity; obtain the bundle and checkout from the trusted repository.

An existing version directory is never overwritten, even if an earlier installation
was incomplete. Failed installation checks preserve the previous `current` selection.
Use `--install-root /absolute/path` for a separate installation, `--python /path/to/python`
to choose its interpreter, or `--offline` when all locked dependencies are already
cached by `uv`. Keep installed version directories intact while their sessions run.

To upgrade, repeat with the new tag and a fresh download directory. Updating `current`
affects future commands; existing supervisors retain their original interpreter.
Coordinate any `up --refresh` with the worktree's owner because it restarts local
connections and reruns Compose. Do not refresh another seat's stack during an upgrade.
For an automation run that must remain on one version while `current` changes, set
`PODGROVE_BIN` to the receipt's exact `executable` path.

### Install from a source checkout

Clone the source if needed, install an isolated copy of the package, then run it from the application worktree:

```sh
git clone https://github.com/podgrove/podgrove.git
cd podgrove
uv tool install .
export PODGROVE_BIN="$(command -v podgrove)"
"$PODGROVE_BIN" --version
```

If the executable is not on your PATH, run `uv tool update-shell` and open a new shell. Pip users can create a separate virtual environment and run `python -m pip install /absolute/path/to/podgrove` in it. Avoid editable installations for shared automation: source edits must not change the runtime used by other worktrees. [Release pinning and upgrades](docs/releasing.md).

### Homebrew distribution

The planned install command is:

```sh
brew install podgrove/tap/podgrove
```

**The Homebrew tap is still being prepared. This command is not available yet.** The [release runbook](docs/releasing.md) explains how a verified release becomes a reviewed formula update. Use the verified release installer or source installation above.

`PODGROVE_BIN` should point to the absolute installed executable. Keep its environment available while worktrees run: background sessions use the interpreter that launched `up`, with Python import isolation so the caller’s directory and ambient import paths cannot select another checkout. Coordinate upgrades and `up --refresh` with active users. Development requires the documented [editable installation](#run-the-tests); setting `PYTHONPATH` to uninstalled source does not select the supervisor runtime.

## Configure the target and prepare access

An administrator must provide an **existing approved namespace**, a kubeconfig identity with the required namespace-scoped permissions, admission for a privileged Docker engine, approved dynamic storage, and a NetworkPolicy-capable cluster. Shared node placement still requires a privileged engine. Podgrove uses only namespaced Kubernetes APIs: it never reads or mutates Namespace, node, StorageClass or PV objects, and it never installs cluster RBAC or admission policies.

Save the target in the **application worktree's `podgrove.yml`**. Replace these example values with your approved kubeconfig context, existing development namespace, and StorageClass:

```yaml
cluster:
  context: your-development-context
  namespace: your-development-namespace
  namespace_mode: shared
  storage_class: your-delete-storage-class
```

There is no default namespace or testing-namespace allowlist. A missing namespace stops the command with `No namespace provided` before Compose, cluster access, or browser startup. `namespace_mode` defaults to `shared`; choose `worktree` explicitly for a derived namespace per worktree, as described under [Namespace modes](#namespace-modes). The platform must prepare that actual derived namespace too.

From that same worktree, generate a dedicated folder of concrete Kubernetes manifests:

```sh
"$PODGROVE_BIN" bootstrap --output /path/to/new-bootstrap-folder
```

The parent directory must exist and the output folder must be new. Generation is offline and needs no Compose file or cluster access. **It cannot inspect the namespace, existing same-name objects, or other workloads.** It prints the actual target and apply command. The five YAML files contain nine namespaced objects: a provisioning ConfigMap, a deny NetworkPolicy selecting only Podgrove-managed Pods, two ServiceAccounts, two Roles and three RoleBindings. It emits no Namespace, ClusterRole, ClusterRoleBinding, admission policy, ResourceQuota or LimitRange. Bootstrap does not require or inspect the StorageClass. See the [administrator guide](deploy/README.md) for exact permissions and installation review.

By default the human binding names the Kubernetes group `podgrove-developers`. The administrator must map the developer's authenticated kubeconfig identity to that group, or generate with `--developer-group your-approved-group`. Applying RBAC does not create authentication or narrow a more privileged identity. Alternatively, the administrator can supply a separate private kubeconfig for the generated `podgrove-client` ServiceAccount, with the intended API server/CA and an approved short-lived credential. Select that kubeconfig using the normal `KUBECONFIG` mechanism and its context in YAML; do not copy administrator credentials or add cluster grants to make checks pass. [Authentication setup](deploy/README.md#authentication-and-the-selected-kubeconfig).

After checking resource-name collisions and approving the trust relationship with existing workloads, an authorized namespace administrator applies that folder to the **same cluster**:

```sh
kubectl --context your-admin-context --namespace your-development-namespace apply -f /path/to/new-bootstrap-folder
```

The manifests contain explicit namespaces; changing kubectl's current namespace cannot retarget them. Generate a fresh folder for a different target. Keep the applied folder for later retirement. The named `podgrove-bootstrap` ConfigMap records shared/worktree mode and the worktree identity where applicable; runtime validates this marker rather than querying or adopting a Namespace.

An approved existing `default` is supported; bootstrap refuses reserved `kube-*` targets. Its policy leaves unrelated/unlabelled CI Pods unselected. However, Pod creation and workload-management rights remain namespace-wide, and CLI ownership checks are not a hardened tenant boundary. Existing privileged-container restrictions, resource quotas, limits and networking rules must be reviewed by the platform owner; Podgrove does not alter them. The administrator also verifies the selected StorageClass and its reclaim behavior. Namespace-scoped Podgrove cannot prove backing-volume deletion, even after a PVC is gone.

**Upgrading an existing installation:** regenerate, review and apply the current namespaced bundle, including its `podgrove-bootstrap` marker, before `doctor` or `up`. Older manual SA/Role subsets lack that required marker. After installation, run `up --refresh` once for an existing worktree to load the updated supervisor while retaining its engine/PVC data; this re-runs Compose, so coordinate it with active tests. Already-running old supervisors do not reload Python code automatically. `down` and retained diagnostics remain available without the marker. If a platform previously installed broader cluster grants/admission/quota controls from an old bundle, have its administrator review their retirement separately; the new bundle neither emits nor removes those objects.

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

Select the application worktree root containing all required local sources, then create `podgrove.yml` there. This example assumes the project actually uses a single `docker-compose.yml` at its root and the administrator has prepared the target described above:

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

The CLI's `--project-directory` chooses the **whole worktree**, its environment identity, and its local-source boundary. It defaults to the current directory. Run subsequent commands from that same root, or supply it explicitly after the subcommand.

`compose.project_directory` chooses the **Compose path-resolution base inside that worktree**, defaulting to `.`. Config filenames, `compose.files`, and `compose.env_file` are relative to the worktree root. Paths inside Compose use the Compose base. A nested `-f` filename alone does not change that base.

For an independent test stack under `tests/`, use this `compose` section while keeping the worktree root at the application root:

```yaml
compose:
  files: [tests/docker-compose.test.yml]
  project_directory: tests
```

A bind such as `../services:/app/services` then stays within the worktree. The [configuration reference](docs/configuration.md#two-directory-settings) explains both directory settings.

Check these before startup:

- Required build, bind, config, and secret sources must exist within the worktree. Synchronized sources cannot contain symlinks or special files; external paths and `.git` sources are refused. Broad binds can include large generated trees, virtual environments, and symlinked dependencies: Podgrove does not silently ignore them.
- The mirror preserves numeric UID/GID and file modes. A local owner-only file may be unreadable to the application's non-root Linux user; prepare compatible development sources rather than assuming Docker Desktop's sharing behavior.
- Existing laptop images and named-volume databases are not migrated. The remote engine builds/pulls images and starts with fresh named volumes; those volumes persist on its PVC until cleanup. Seed or import test data through the project's normal workflow.
- Laptop services, host devices, private-network dependencies, and external Docker resources do not move with the worktree. Default engine egress allows DNS and public IPv4 HTTP(S); other destinations require reviewed platform changes. Unsupported Compose features fail explicitly; consult [known limits](docs/known-limits.md#refused-compose-features).

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

The engine's PVC request comes from `storage.size` in `podgrove.yml`, defaulting to `20Gi`; `up --storage` overrides it for that invocation. Save a custom capacity in YAML so normal launches, refreshes and reconnects use it consistently. `cluster.storage_class` supplies the startup class; `up --storage-class` overrides it. A new PVC requires an explicit administrator-approved class. Reconnect can reuse the class already recorded on its owned PVC. Podgrove never discovers default classes or reads StorageClass/PV metadata. Bootstrap needs no class and grants no cluster storage access. Resource or PVC configuration changes do not resize an existing environment; see [custom resources](#custom-resources-and-storage).

### 4. Verify the adopted project

Before declaring adoption complete:

1. Check `status` and the application's declared readiness, including any initialization jobs.
2. Run the project's HTTP, WebSocket, database, and business tests against the **printed laptop endpoints**. Do not reuse the old local Compose port numbers blindly.
3. Make a controlled source edit covered by a bind mount or declared watch rule and verify the remote application observes it. Application reload behavior remains the project's responsibility. Revert the edit afterward.
4. Write disposable test data, run `up --refresh`, and verify it survives on named volumes. Refresh re-runs the build/start workflow while retaining the engine/PVC; it is not a request to erase data.
5. Save needed diagnostics, run `down` when finished, and verify the owned resources are removed. Record the actual commands, results, and anything blocked. A dry-run, dashboard screenshot, or fixture pass alone is not end-to-end application acceptance.

## Endpoints and live edits

By default, Podgrove forwards every published TCP port to `127.0.0.1`. Target-only declarations such as `ports: ["27017"]`, zero-valued publishers, and published ranges use Docker’s observed allocation after startup. Each forwarded target must resolve to exactly one publisher on one active replica. `expose` alone creates no endpoint. If Docker changes or removes a publisher later, Podgrove disconnects those application forwards and asks for `up --refresh`; it never keeps forwarding to a stale mapping. For a Compose mapping `8080:8000`, a forwarding entry uses the **container target `8000`**:

```yaml
forward:
  - service: api
    port: 8000
```

This fragment assumes `api` exists and publishes that target. Omit `forward` for automatic selection, supply a list to restrict it, or use `forward: []` to disable application tunnels. Optional `local: 43123` fixes a laptop port; normally omit it so concurrent worktrees can choose available ports. An occupied explicit port is an error. Automatic ports prefer a stable worktree-specific number and choose another if needed. Printed `http://` addresses are conveniences; Redis, MongoDB, TLS, and other TCP services still need their own client/protocol.

Bind/config/secret files are copied before service startup, then local changes are mirrored one way. Remote edits do not sync back. Image-only source needs a declared Compose `develop.watch` action or an explicit `up --refresh`; Podgrove does not invent reload rules. A declared watch rebuild action can rebuild an image. Build contexts still follow the project's `.dockerignore`. See [file-sync architecture](docs/how-it-works.md#storage-and-file-sync).

To exclude local generated files from the mirror, opt in explicitly:

```yaml
sync:
  exclude: [__pycache__, "*.pyc", .pytest_cache]
```

Slashless patterns match any path component; slash-containing patterns are relative to the selected workspace root, with `**` matching nested directories. An excluded directory excludes its descendants. Absolute paths, `..` and negation are refused. An explicitly mounted/config/secret source cannot itself be excluded. Exclusions do not change Compose build contexts or native watch rules, and do not delete already mirrored or remotely generated files. There are no implicit `.gitignore` rules; `.git` remains forbidden. Add dependency/cache directories only when your containers do not need their local contents.

Files changing while a local snapshot is prepared are retried with backoff without shutting down the application forwards. A failed or uncertain remote transfer is not replayed automatically. Forwarding has an independent monitor that restarts a dropped process/listener on the **same local ports**, verifies the original engine's ownership and UIDs, and reports `reconnecting` while retrying. Three failed retries leave endpoints `disconnected` and the session `degraded`; run `up` to reconnect. Pod replacement or ownership changes are never silently adopted. A listening forward proves local tunnel availability, not application health. A failed ownership read marks forwarding `reconnecting` while keeping the existing verified listener during a bounded 120-second grace period. Proof expiry closes it; only a fresh matching UID check can authorize reconnection. Lease heartbeat failures are reported separately and retried from a fresh ownership read with capped backoff.

## Lifecycle and target selection

Run commands from the application worktree or any directory inside it, or pass `--project-directory /path/to/application-worktree` after the subcommand. Podgrove derives identity from the nearest Git checkout’s top level, including linked worktrees; selecting a subdirectory does not create another environment. It discovers the nearest `podgrove.yml` upward within that checkout. The selected configuration directory and Compose base remain separate from identity; an explicit `--config` is resolved against the selected directory. Outside Git, the selected directory remains the identity boundary. Replace `api` and the executable with a real service/command in your project:

```sh
"$PODGROVE_BIN" status
"$PODGROVE_BIN" status --all --json
"$PODGROVE_BIN" env --json
"$PODGROVE_BIN" logs --follow api
"$PODGROVE_BIN" exec api -- python -V
"$PODGROVE_BIN" up --refresh
```

Calling ordinary `up` again returns an existing running session when its normalized Compose configuration is unchanged. Fingerprints use deterministic serialization; the earlier unversioned format is also recognized when omitted network/sync settings still have their original defaults. Changed configuration re-runs Compose against the same engine/PVC. Use `--refresh` for build-only source or service env-file changes that do not change that model. Run `up` again after a disconnected session. Existing engines/PVCs retain their size and placement; flags do not resize them. Recreating with `down` destroys their data.

Status includes the worktree identity/root and each forward's state, separately from Compose service health. `engine_identity` records captured and observed Pod/StatefulSet UIDs, so replacement remains visible even when the new Pod reports zero restarts. Failed builds make a fresh identity check and report replacement without repeating the build. Transient Docker status reads receive three bounded attempts. Exhaustion reports valid JSON with degraded/stale health and keeps the session and forwards available for a later observation. `down --json` returns a JSON cleanup result; cleanup errors return a JSON error and preserve the state needed to retry. `env` exports only ready endpoints as `PODGROVE_<SERVICE>_<TARGET_PORT>_HOST`, `_PORT` and `_URL`; `--json` returns a JSON mapping. Shell output contains quoted `export` statements. `_URL` uses HTTP as a convenience; build MongoDB/Redis/TLS connection strings using your application's protocol and test credentials. Normalized service-name collisions are refused; `status --json` retains original service names.

After a failed startup disconnects, `logs SERVICE` opens a temporary, ownership-checked Docker connection to the retained engine. Newly recorded environments retain the original Compose project/service names, so logs still work when the Compose sources have since changed or disappeared; pass explicit context and namespace if YAML itself is invalid. This diagnostic command neither rebuilds nor restarts containers, and closes the temporary connection on exit. The engine must still exist. Older records without project metadata require their original valid Compose configuration.

`exec` streams stdin, stdout and stderr directly through an owned Kubernetes exec using WebSockets, preserving binary output and the remote exit status. It selects the exact Compose project/service container, verifies the engine and PVC identities, and does not replay interrupted commands. Inspect the command’s effects before retrying after an interruption.

Existing environments created by an older version from a Git subdirectory keep their original identity. The new resolver refuses to silently adopt or merge those records: use the recorded version and original directory to inspect or retire that environment before starting from the worktree root. Keep the previous versioned installation available during upgrades.

Target precedence is per field: an explicit CLI flag **after the subcommand**, then YAML, then `PODGROVE_CONTEXT` for context only. Namespace has no implicit fallback. An explicit `--namespace` is an exact target and selects shared mode unless accompanied by `--namespace-mode worktree`; this allows recovery using an already-derived namespace without deriving it twice. The current kube context is never an implicit target; Podgrove does not select or modify Docker contexts.

Always use the intended context. Local records are keyed by worktree and context, so changing `cluster.context` selects a different record. Within the same resolved context, existing `status`, `logs`, `exec`, and `down` retain the recorded namespace; changing YAML namespace cannot redirect those operations. A mismatching explicit `--namespace` is refused. If the default YAML becomes invalid, explicit `--context` and `--namespace` together allow target-only commands such as `down` to bypass it; an explicit `--config` is still validated.

`status --all` lists this machine's saved environments for the resolved context and namespace. It needs no application worktree when its exact target is supplied explicitly. It does not discover other machines' environments or query every stack's current service health; use ordinary `status` for that worktree.

## Namespace modes

Podgrove supports both one namespace per worktree and multiple worktrees in a single namespace:

| Mode | Configuration for a new environment | Resources and cleanup |
| --- | --- | --- |
| Shared namespace | Set `cluster.namespace` to your namespace and use `cluster.namespace_mode: shared` (the default mode). | Have the platform prepare that namespace, then bootstrap once for it. Multiple worktrees get separate engine Pods, StatefulSets, and PVCs inside it. `down` retains the namespace and bootstrap resources. |
| Namespace per worktree | Set `cluster.namespace` to a base name and `cluster.namespace_mode: worktree`. | The target is `<base>-wt-<worktree-id>`; long bases are shortened with a hash. Have the platform prepare each derived namespace, then generate/apply bootstrap from **each worktree**, using a separate output folder for each. `up`, `doctor`, `web`, and cleanup resolve the same target. `down` retains the existing namespace and its bootstrap access setup. |

In either mode, each worktree's Compose services run inside its own Docker engine, with separate networks and named volumes. Compose services are not individual Kubernetes Deployments. Sharing a namespace does not mean sharing an engine or PVC.

For example, keep the context and StorageClass fields from the setup above and change the namespace settings to:

```yaml
cluster:
  context: your-development-context
  namespace: my-team
  namespace_mode: worktree
  storage_class: your-delete-storage-class
```

Run `bootstrap --output /path/to/worktree-a-bootstrap` from worktree A and generate a different folder from worktree B. An administrator first prepares each printed actual namespace through the platform process, then applies its folder to the selected cluster. Applying A's folder does not grant access to B's namespace. The CLI does not grant itself namespace-creation or RBAC-administration permissions. In shared mode, all worktrees using the same target reuse one installation.

For two existing application worktrees with `namespace_mode: worktree` in their respective `podgrove.yml` files, generate from each exact root. Replace the paths, and keep the output outside both worktrees:

```sh
mkdir -p /path/to/platform-manifests
"$PODGROVE_BIN" bootstrap --project-directory /path/to/worktree-a --output /path/to/platform-manifests/worktree-a
"$PODGROVE_BIN" bootstrap --project-directory /path/to/worktree-b --output /path/to/platform-manifests/worktree-b
```

The administrator prepares the two printed namespaces, reviews each folder, then applies them on the configured cluster (each manifest names its exact namespace):

```sh
kubectl --context your-admin-context apply -f /path/to/platform-manifests/worktree-a
kubectl --context your-admin-context apply -f /path/to/platform-manifests/worktree-b
```

Each developer then runs the validation/startup sequence from their corresponding root; for example, `"$PODGROVE_BIN" up --project-directory /path/to/worktree-a`. Namespace identity depends on that resolved local root: generate the bundle using the same root that will run `up`, even when a different administrator applies the files.

This checkout's local `podgrove.yml` selects existing `default`; that is a workspace setting, not a product default. Each application worktree reads its own configuration. Missing `cluster.namespace` is an error in both modes; omitting it never enables per-worktree namespaces.

Namespace mode is independent of `node_mode: shared` or `tainted`, which controls node placement. Namespace names follow Kubernetes DNS-label syntax. Changing namespace or namespace mode requires removing the old environment before starting the new one; `down` deletes its PVC data. To retire a whole platform installation, first stop all its worktrees, then have an administrator run `kubectl --context your-admin-context delete --ignore-not-found -f /path/to/its-bootstrap-folder`. This removes only the generated namespaced access/marker/baseline objects; it never deletes the Namespace or unrelated workloads. Review all users before retiring a shared installation.

## Worktree network isolation

Generated `05-network-isolation.yaml` denies ingress and egress for **Pods labelled `app.kubernetes.io/managed-by: podgrove` only**. Unrelated or unlabelled CI Pods are not selected. Each worktree then gets a separate policy: no incoming Pod traffic, DNS only to `kube-system` Pods labelled `k8s-app: kube-dns` on TCP/UDP 53, and public IPv4 TCP 80/443 for image pulls and build dependencies. It adds no general same-namespace or other-namespace access. Services within one Compose stack communicate inside that worktree's Docker engine; CLI tunnels use authenticated Kubernetes exec/port-forward.

Public-web egress excludes private, shared-address, link-local/metadata, loopback, documentation, benchmark, multicast and reserved IPv4 ranges. **Before deployment, the administrator must review the actual Pod, Service, node and control-plane ranges**, and add any additional infrastructure/public ingress addresses that must be unreachable. Save those values in each worktree's `podgrove.yml`:

```yaml
network:
  blocked_cidrs:
    - 203.0.113.0/24 # Documentation example; replace with actual infrastructure CIDRs.
```

These exclusions supplement the built-in ranges; they never replace them. They restrict the public-web rule, while the narrow DNS exception remains. Use CIDR network addresses without host bits. IPv6 internet egress is not enabled; matching cluster DNS can still use IPv6. If the environment needs no public web access, `blocked_cidrs: ["0.0.0.0/0"]` leaves only the scoped DNS allowance, which also prevents remote image pulls/build downloads from the engine. [Complete example](examples/network/podgrove.yml).

`up` reconciles the engine's owned policy, including when the session is already running; changed network configuration is part of the startup configuration. `down` removes that engine's policy while retaining the administrator-installed deny policy for Podgrove-managed Pods. Existing installations need a newly generated bootstrap folder reviewed/applied to receive the baseline. Optional scheduled reapers need the separate reviewed API/DNS policy in [their templates](deploy/README.md#optional-scheduled-reaper).

These rules need a CNI that enforces NetworkPolicy. Kubernetes allow rules are additive: another broad allow policy can reopen traffic. Node-local traffic, Service address translation and public ingress/proxy paths also require platform review; public HTTP(S) access is not an absolute ban on every public endpoint backed by another namespace. The generated developer permissions can manage namespace policies, and privileged engines share their node kernel. For a stronger boundary, administrators must enforce network rules outside that developer identity using their CNI/admin policy or an approved egress gateway. Validate actual packet paths before treating isolation as established. [Kubernetes policy behavior](https://kubernetes.io/docs/concepts/services-networking/network-policies/).

## Browser dashboard

From an application worktree containing the target configuration:

```sh
"$PODGROVE_BIN" web
```

To develop the dashboard from this checkout, supply an application project directory with `--project-directory`; no developer cluster settings are shipped.

No context or namespace flags are needed when YAML supplies them. A dashboard-only directory needs no Compose file; use the [dashboard-only example](examples/dashboard/podgrove.yml) and set its approved target. Distributed packages do not include this workspace's target configuration. Optional overrides:

```sh
"$PODGROVE_BIN" web --port 8765 --no-open
"$PODGROVE_BIN" web --project-directory /path/to/application-worktree --config podgrove.yml
```

The dashboard binds only to loopback, chooses an available port by default, opens a browser, and stays in the foreground until Ctrl-C. It needs no frontend build. It lists this machine's recorded worktrees for the resolved context/namespace, with branch/worktree identity, Compose services, engine/Pod allocations, claimed PVC capacity and searchable endpoints. The Logs tab offers snapshots or a live connection, pause/resume, wrapping, copying and fullscreen. Select **All logs** to request all retained history for the selected source; snapshots have a 64 KiB limit and report clipping. Live connections last up to five minutes; resuming starts a fresh tail and can repeat or miss lines. Displayed history is bounded to 2,000 lines and 256 KiB, with a visible notice when older lines are trimmed. Export retained service history without these dashboard display limits using `podgrove logs SERVICE --tail all > service.log` (add `--follow` to continue streaming). CLI output comes directly from Compose and is not redacted by the dashboard; already rotated logs cannot be recovered. Allocations are not live resource-usage measurements.

The sidebar's Configuration page separates cluster, access, worktree and resource settings into tabs. It shows safe current YAML settings, saved network exclusions, observed engine/initializer allocations and PVC capacity, the selected context/namespace, provisioning-marker metadata, and scoped observed ServiceAccount/RBAC declarations. It does not read Namespace or cluster-RBAC metadata. These declarations do not prove effective permissions or that bootstrap was installed. Viewing/refreshing does not extend TTL or change environments; saved health is labelled as a snapshot. Common credential patterns in logs are redacted, but arbitrary log secrets may still appear. See the [dashboard guide](docs/web.md).

## Node placement and resource sizes

`node_mode: shared` schedules on eligible existing Linux EC2 nodes, excludes Fargate and EKS Auto Mode, and adds no tolerations for existing taints. Neither mode reads node inventory. Optional `node_mode: tainted` selects an existing pool using `tainted_nodes.selector` plus a matching taint key/value/effect. The platform owner supplies compatible selector/taint values; Podgrove reports scheduling outcomes through its owned Pod status. See [node placement](docs/configuration.md#node-placement) for a complete example.

Tainted placement adds no cluster permissions and has no node-reader bootstrap option. The administrator must arrange the approved pool and confirm capacity outside Podgrove; changing `node_mode` only changes the engine Pod scheduling request.

Both modes keep separate engines/PVCs, ownership checks, NetworkPolicies, and the configured resource requests/limits. The Docker engine is privileged and shares its node's kernel; namespace separation is not a hardened boundary against malicious workloads. No CLI command creates or modifies nodes.

When `resources` is absent, `size` selects one of these engine presets:

| Engine size | CPU request / limit | Memory request / limit |
| --- | --- | --- |
| `small` | `250m` / `2` | `2Gi` / `2Gi` |
| `medium` (default) | `1` / `4` | `8Gi` / `8Gi` |
| `large` | `2` / `8` | `16Gi` / `16Gi` |

Each preset also sets an engine ephemeral-storage limit of `4Gi` with no explicit ephemeral-storage request. Preset memory requests equal their limits. The budgets cover the Docker daemon, builds, and every nested Compose service together; existing cluster autoscaling may add capacity if eligible nodes cannot fit the request. Per-service settings such as `cpus`, `mem_limit` and `deploy.resources` stay in the original Compose files and share that engine budget.

### Custom resources and storage

Put persistent sizing choices in `podgrove.yml` alongside the existing target and Compose settings:

```yaml
resources:
  requests:
    cpu: "1500m"
    memory: 6Gi
    ephemeral-storage: 1Gi
  limits:
    cpu: "6"
    memory: 12Gi
    ephemeral-storage: 8Gi
storage:
  size: 40Gi
```

Each worktree loads its own `podgrove.yml`, so worktrees sharing a namespace can use different resource allocations and PVC capacities. An explicit `resources` block **replaces the entire engine preset**. Podgrove sends only the requests and limits you declare; it does not fill missing dimensions from `size`. A requests-only block adds no Podgrove limits, and `resources: {}` adds neither requests nor limits. YAML `size` is used only when `resources` is absent; an explicit `up --size small` overrides YAML resources with that preset. There is no Podgrove-specific CPU, memory, ephemeral-storage or PVC upper ceiling. Resource quantities must be nonnegative and representable by Kubernetes; PVC capacity must be positive. CPU must use at least `1m` precision, and a request cannot exceed its corresponding limit. An explicit zero request is retained rather than defaulted from a positive limit.

The optional `init_resources` block independently replaces resources for the storage initializer using the same rules, including `init_resources: {}`. If omitted, it retains requests of `10m` CPU / `16Mi` memory and limits of `100m` CPU / `32Mi` memory. See the [complete example](examples/resources/podgrove.yml) and [configuration reference](docs/configuration.md#resource-sizes) for all fields and precedence.

Omitted fields do not bypass namespace LimitRanges, quotas or admission rules. Kubernetes can supply defaults, including copying a limit into an omitted request when no admission default supplies one; actual admitted allocations may therefore differ from the YAML. [Kubernetes resource behavior](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/#requests-and-limits). Use `up --dry-run --json` to inspect the requested manifest and `doctor` to check admission before a new deployment.

Existing engine, initializer and PVC compatibility is checked before an active session is stopped. Incompatible settings are refused; `up --refresh` neither resizes nor silently recreates them. Restore the existing settings to reconnect, or plan a separate environment and data migration for a different allocation. **`down` deletes the PVC and its data**, so it is not a routine sizing update. PVC capacity covers the worktree mirror, Docker data and named volumes; `ephemeral-storage` covers the engine container's local storage separately.

## Cleanup and idle expiry

**`down` deletes this environment's PVC and database contents.** It also removes the matching local state, session log, socket, and temporary files after successful cluster cleanup. Save diagnostics first:

```sh
"$PODGROVE_BIN" down
```

Failed cleanup retains state for retry. Cleanup selects resources bearing both Podgrove's management label and the environment identity. All modes, including old recorded exclusive environments, retain namespaces and bootstrap resources. Podgrove never deletes a Namespace. Backing-volume cleanup depends on the administrator-approved storage provisioner and reclaim policy; a gone PVC alone is not proof that PV/cloud storage is gone, and Podgrove cannot inspect those cluster resources.

The connected background session checks idle TTL and, optionally, an explicitly supplied GitLab.com MR URL (`up --mr-url URL`). Local bind/config/secret/watch edits and successful `status`, `logs`, or `exec` refresh activity. **Dashboard viewing and application/test traffic do not extend TTL.** Choose enough time for unattended tests. Verified MR closure/merge triggers cleanup; API failures retain the environment.

A sleeping/disconnected laptop cannot run cleanup. Arrange a separate reaper if required. With context and namespace configured in the current directory's YAML:

```sh
"$PODGROVE_BIN" reap --dry-run
"$PODGROVE_BIN" reap --watch --interval 60
```

The reaper requires a namespace from YAML or `--namespace` and never scans arbitrary namespaces. **By default it considers only the current worktree identity.** Use `--environment <12-character-identity>` for another exact environment, or explicit `--all` for every Podgrove environment in the selected namespace. These flags do not force deletion before cleanup conditions are met. Multiple seats sharing one OS user also share the default local state directory; use separate worktree roots and, if desired, `PODGROVE_STATE_HOME` directories. Namespace-wide `--all` remains broad even with separate local state. Removed-worktree cleanup requires matching private state on the reaper's machine and an existing parent directory. A remote reaper cannot infer another laptop's filesystem state. Reaping is not restricted to disconnected sessions: expired connected stacks can also be removed. See [lifecycle details](docs/configuration.md#lifecycle-and-environment-variables) and the separately reviewed [scheduled reaper](deploy/README.md).

For two-repository backend/web lanes, stable Compose interpolation, endpoint exports and a test procedure for your approved cluster, read [development workflows](docs/dogfood-workflows.md).

## Run the tests

Run repository checks from the **Podgrove checkout**. Install both test extras and a browser for the UI checks:

```sh
uv sync --locked --extra test --extra web-test
.venv/bin/python -m playwright install chromium
.venv/bin/pytest -q -m 'not integration and not cluster'
.venv/bin/ruff check podgrove tests scripts
```

Pip users can install `-e '.[test,web-test]'` with the same virtual environment instead. The browser tests use a fresh disposable profile and inert local fixtures; they do not use Kubernetes or Docker. They can use installed macOS Chrome, Playwright Chromium, or `PODGROVE_BROWSER_EXECUTABLE`. Missing Playwright/browser dependencies cause skips, so inspect the test summary rather than counting a skipped browser suite as passed.

Opt-in local Docker checks, requiring a local Unix-socket engine with privileged-container support:

```sh
PODGROVE_DOCKER_TESTS=1 .venv/bin/pytest tests/test_sync.py -q
export PODGROVE_E2E_OUTPUT="$(mktemp -d /tmp/podgrove-docker-e2e.XXXXXX)"
.venv/bin/python scripts/e2e.py --mongo --output "$PODGROVE_E2E_OUTPUT"
```

The sync tests use disposable Docker volumes. The end-to-end harness creates bounded, uniquely named Docker-in-Docker engines, compares two isolated worktrees, and cleans up its resources. It never invokes Kubernetes. `--mongo` adds MongoDB isolation to Redis and application checks; results and command logs go to the fresh output directory. Keep any evidence you need; do not reuse a directory containing an earlier run's results.

To include both Docker lanes (including Mongo) in the complete local pytest run:

```sh
PODGROVE_DOCKER_TESTS=1 PODGROVE_RUN_DOCKER_E2E=1 .venv/bin/pytest -q -m 'not cluster'
.venv/bin/python scripts/verify_docs.py --output /tmp/podgrove-docs-new.json
```

The documentation report path must be new. `scripts/verify_package.py --output /tmp/podgrove-package-new` builds and verifies installed wheel/source archives offline using locally available build/dependency files. Optional `--deliver` preserves the previous matching archives before placing the verified pair in `dist/`; run it after source and documentation stop changing.

### Cluster acceptance

Local tests do not establish your cluster's admission, storage or NetworkPolicy behavior. Follow the [administrator guide](deploy/README.md), use an explicitly approved disposable namespace, and verify application health, allowed DNS/egress, denied peer traffic, PVC persistence and exact owned cleanup. Podgrove never supplies a default test cluster or authorizes production changes.

See [verification](docs/verification.md) for repeatable test lanes, [architecture](docs/how-it-works.md) for implementation details, and [design choices](docs/why-not.md) for the rationale.

## Contributing and releases

Read [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and the [release runbook](docs/releasing.md). Releases use Conventional Commits, Release Please, verified Python archives and reviewed Homebrew formula updates. The approved logo and its variants are documented in [branding](docs/branding.md).

## License

[MIT](LICENSE), matching the project's open-source distribution. Dependency licenses remain their own.
