# Configuration reference

`podgrove.yml` holds environment settings around an existing Compose project. It never defines services, networks, or volumes. Commands require an explicit namespace from YAML or a flag; cluster operations also require an explicit context. Local `validate` and `up --dry-run` need no context, but still require a namespace. The schema permits partial configuration fragments because flags can supply missing values. The versioned machine-readable schema is [podgrove-v1.schema.json](../schema/podgrove-v1.schema.json).

```yaml
version: 1
cluster:
  context: your-explicit-kube-context
  namespace: my-development
  namespace_mode: shared
  storage_class: your-delete-storage-class
compose:
  files:
    - compose.yml
    - compose.dev.yml
  profiles: [frontend]
  env_file: .env.example
  project_directory: .
forward:
  - service: api
    port: 8000
  - service: frontend
    port: 3000
    local: 43123
size: medium
node_mode: shared
ttl: 8h
```

The sample assumes those files, profiles, services, and published ports exist in your Compose project.

## Fields

| Field | Default | Meaning |
| --- | --- | --- |
| `version` | `1` | Only version 1 is accepted. |
| `cluster.context` | `PODGROVE_CONTEXT` when no flag/YAML value is supplied | Explicit Kubernetes context name. `--context` overrides this field; current kubeconfig context is never inferred. |
| `cluster.namespace` | Required; no fallback | Existing shared namespace, or the base for worktree mode. Valid Kubernetes DNS label, at most 63 characters. Missing namespace stops before external work. |
| `cluster.namespace_mode` | `shared` | `shared` uses the named namespace; `worktree` derives `<base>-wt-<identity>`. Both require administrator-prepared namespaces and retain them on cleanup. |
| `cluster.storage_class` | Existing owned PVC class on reconnect; required for a new PVC | Administrator-approved class; `up --storage-class` overrides it. Bootstrap needs no class and grants no StorageClass/PV reads. |
| `compose.files` | Discovered standard base plus optional override | Nonempty, ordered list of Compose files, passed as repeated `--file` arguments. |
| `compose.profiles` | `[]` | Profiles passed directly to Compose. |
| `compose.env_file` | Compose's normal `.env` behavior | A local interpolation env file, passed as Compose's `--env-file`; this is separate from a service's `env_file`. |
| `compose.project_directory` | `.` | Compose's path-resolution base, inside the worktree. |
| `forward` | Every published TCP port | The subset of published service ports to expose locally. `[]` disables application tunnels. |
| `forward[].service` | Required per entry | An enabled Compose service. |
| `forward[].port` | Required per entry | The service's **container target port**, not its published daemon port. |
| `forward[].local` | An available worktree-specific port | An explicit loopback port, from 1 through 65535. An occupied explicit port is an error. |
| `sync.exclude` | `[]` | Explicit workspace-relative globs excluded from bind/config/secret mirrors; no implicit gitignore, no changes to Compose build/watch rules. |
| `size` | `medium` | Engine preset (`small`, `medium`, `large`), used only when `resources` is absent. Explicit `up --size` overrides YAML resources. |
| `resources` | The selected `size` preset | Complete replacement of the engine's requests/limits. An explicit empty map adds none. |
| `resources.requests` / `resources.limits` | Omitted when not declared in an explicit `resources` block | Maps accepting `cpu`, `memory`, `ephemeral-storage`; nonnegative Kubernetes quantities, with request no greater than the corresponding limit. Explicit zero is preserved. |
| `init_resources` | Requests: CPU `10m`, memory `16Mi`; limits: CPU `100m`, memory `32Mi` | Complete replacement of the storage initializer's requests/limits, with the same shape and validation as `resources`. An empty map adds none. |
| `storage.size` | `20Gi` | Positive Kubernetes storage quantity for the engine PVC. `up --storage` overrides it for that invocation; existing PVCs are never resized. |
| `node_mode` | `shared` | `shared` uses eligible existing Linux EC2 nodes; `tainted` supplies a configured pool selector/toleration without reading nodes. |
| `network.blocked_cidrs` | `[]`, in addition to built-in exclusions | Up to 128 distinct IPv4/IPv6 CIDR network addresses. Adds infrastructure exclusions to the engine's public IPv4 HTTP(S) rule; DNS remains a separate scoped exception. IPv6 public egress is not enabled. |
| `tainted_nodes.selector` | `{podgrove.dev/dedicated: "true"}` | Nonempty map of node label names to string values; a supplied map replaces the default. |
| `tainted_nodes.taint.key` | `dedicated` | Taint key placed in the Pod toleration; the administrator verifies taints on the selected nodes. |
| `tainted_nodes.taint.value` | `podgrove` | Expected taint value; an explicit empty string is accepted. |
| `tainted_nodes.taint.effect` | `NoSchedule` | `NoSchedule` or `NoExecute`; used by the Pod toleration; cluster taints are not read. |
| `ttl` | `8h` | Positive integer followed by `s`, `m`, `h`, or `d`, for example `30m` or `2d`. |

When no `compose.files` or `-f` is supplied, discovery starts at `compose.project_directory` and searches upward within the worktree. At each level it checks `compose.yaml`, `compose.yml`, `docker-compose.yml`, then `docker-compose.yaml`. Once a base is found, it automatically adds the first existing standard override beside that base: `compose.override.yml`, `compose.override.yaml`, `docker-compose.override.yml`, then `docker-compose.override.yaml`. Explicit `compose.files` or repeated `-f` arguments remain the exact ordered file list; no additional override is added to that list.

Unknown keys, duplicate YAML keys, invalid values, duplicate forwarding targets, and duplicate explicit local ports are errors. A message names the offending field. Podgrove checks the configuration and normalized Compose model before creating Kubernetes resources.

## Cluster target and dashboard-only configuration

A directory can contain only this file and still open the dashboard with `podgrove web`:

```yaml
cluster:
  context: your-explicit-kube-context
  namespace: my-development
```

Target precedence is per field: explicit CLI flag, then YAML, then `PODGROVE_CONTEXT` for context only. Namespace has no fallback; neither `default` nor a generated worktree namespace is assumed. A missing namespace or context is an error before opening the dashboard or contacting a cluster. The current kubectl context is never read implicitly. An explicit `--namespace` is an exact target and selects shared mode unless accompanied by `--namespace-mode worktree`; this permits recovery using a resolved worktree namespace without deriving it twice.

`web --project-directory /path/to/worktree --config settings/dashboard.yml` resolves the explicit config path beneath the selected directory. Without `--config`, Podgrove searches for the nearest `podgrove.yml` upward within the Git worktree. The target reader validates YAML settings but does not load referenced Compose files, interpolation env files, services or Docker configuration. A dashboard-only target does not need a Compose project. The same target fields are used by new `up`, `doctor`, `reap` and `status --all`; `reap` still requires a namespace supplied by config or a flag and never discovers arbitrary namespaces.

For existing `status`, `logs`, `exec` and `down`, the validated state record controls the environment namespace and recorded namespace mode in the selected context. A changed `cluster.namespace` cannot redirect existing cleanup; a mismatching explicit `--namespace` is refused. Changing context selects a different state record. With no local state, the explicit configured/flagged namespace supplies the narrowly scoped recovery target and an owned lease can recover its lifecycle mode. For target-only commands such as `web`, `status` and `down`, supplying both `--context` and `--namespace` avoids reading the default YAML and permits recovery when that file is broken. An explicitly selected `--config` remains subject to validation. `up` always loads and validates the full project configuration.

## Namespace modes and bootstrap

Set `cluster.namespace_mode: shared` to reuse the configured namespace across worktrees. Set it to `worktree` to derive `<cluster.namespace>-wt-<worktree-identity>` from the same root used for all commands. Bases longer than 47 characters are shortened with a hash of the full base, keeping the result within 63 characters and avoiding truncation collisions. Namespace is mandatory in both modes. Node placement (`node_mode`) is a separate choice.

Generate the installation from the selected worktree with `podgrove bootstrap --output /path/to/new-folder`. It reads the target without loading Compose sources or contacting the cluster. StorageClass is not required for generation. The output folder must be new beneath an existing ordinary parent. It contains five files with nine namespaced objects, ready for review and the printed `kubectl --context ... --namespace ... apply -f ...` command. Offline generation cannot inspect namespace occupants or same-name resource collisions.

Shared mode needs one installation per existing namespace. Worktree mode needs a separately prepared actual namespace and generation/application **for each worktree**, in separate folders. A namespaced ConfigMap `podgrove-bootstrap` records `version: "1"`, `namespace_mode`, and worktree-only `environment` identity. It has management/component labels but no environment ownership label, so cleanup retains it. Runtime checks this marker instead of reading or changing Namespace metadata.

The bundle contains only the marker, a managed-Pod deny policy, two ServiceAccounts, two Roles and three RoleBindings. It emits no Namespace, cluster RBAC, admission policy, quota or limit range. `--developer-group` selects the approved human group; authentication/group mapping is arranged by the administrator. There is no node-reader grant or option. Approved existing `default` is supported; bootstrap refuses reserved `kube-*` namespaces.

Every mode retains the existing namespace and bootstrap on `down` or reaping, including old records marked `exclusive`. Only owned environment resources/PVC data are removed; new configuration cannot select the legacy mode. To retire an installation, remove its environments, review all users, then have an authorized namespace administrator delete the saved bundle. That removes only its named setup objects, never the Namespace or unrelated workloads. See the [administrator guide](../deploy/README.md).

## Network settings

Generated bootstrap includes a retained deny NetworkPolicy selecting only Pods labelled `app.kubernetes.io/managed-by: podgrove`; unrelated and unlabelled CI Pods are unselected; each engine receives its own narrowly selected DNS/public-web allow policy. Administrators must review the actual Pod, Service, node, API and protected ingress addresses, particularly non-private ranges, and supply extra exclusions through `network.blocked_cidrs`. CIDRs must use network addresses without host bits, contain an explicit prefix, and have no scope identifier. IPv6 forms are normalized; duplicate normalized values and unknown settings are refused before external operations.

```yaml
network:
  blocked_cidrs:
    - 203.0.113.0/24 # Replace this documentation range with actual infrastructure CIDRs.
```

Built-in private/special IPv4 exclusions always remain. Adjacent/overlapping IPv4 ranges are consolidated in the generated policy. Setting `0.0.0.0/0` removes public-web egress entirely, retaining only the explicit DNS allowance. IPv6 CIDRs can be recorded, but do not create an IPv6 public-egress rule. Bootstrap itself is deny-only and grants no competing broad allowance; these additional values are used by the worktree policy rendered by `up --dry-run` and installed by `up`.

Run `up` after changing exclusions. It also reconciles a missing or modified owned policy before returning an already-running environment. Provisioning-marker validation, policy ownership and optimistic concurrency prevent adopting or overwriting a replacement object. Policies added by other actors remain an administrator concern: standard NetworkPolicy allows combine, and Service/NAT/node exceptions vary by platform. See [network isolation and its limits](../README.md#worktree-network-isolation) and the [administrator guide](../deploy/README.md).

## Two directory settings

The CLI's `--project-directory` selects a directory to locate the project configuration. It defaults to the current directory. Inside Git, the nearest checkout's top level determines environment identity even when this flag explicitly names a subdirectory. Linked worktrees have separate identities despite sharing Git metadata; branch names and environment variables do not choose identity. Outside Git, the selected directory itself determines identity. Missing paths retain their selected-path identity for recorded cleanup.

Without `--config`, the nearest `podgrove.yml` is found upward from the selected directory, stopping at the Git worktree boundary. Its directory is the configuration and allowed-source boundary. If no config exists, the selected directory remains that boundary. An explicit `--config` is resolved beneath the selected directory and retains that directory as its boundary; it cannot escape through `..` or a symlink. Podgrove records this configuration directory separately from the worktree identity so status, logs, the supervisor and the dashboard can reload the same configuration.

`compose.project_directory` chooses the Compose path-resolution base **within** the configuration boundary. It does not change identity or enlarge the sync boundary. `compose.files` and `compose.env_file` are resolved from the configuration directory; paths inside Compose are resolved by Compose against its selected project directory.

Commit one `podgrove.yml` at the repository root with relative Compose paths. Every linked worktree can use identical bytes: engine/PVC names, namespace suffixes and loopback ports are derived at runtime. Do not insert a worktree identity, branch name or absolute checkout path into the committed YAML. The [complete portable example](../examples/portable/README.md) and its [real linked-worktree regression](../tests/test_portable_worktrees.py) cover both namespace modes. Cluster target fields are team configuration; replace the example values with approved settings before deployment.

Older releases could create an environment for a Git subdirectory. If one of those records exists, the new resolver refuses to adopt, merge or duplicate it and identifies its recorded version and directory. Resolve that old environment with its original version before using the new identity. Existing environments already rooted at the checkout top level retain their identity.

For example, with `podgrove.yml` at a backend repository root and the test Compose file under `tests/`:

```yaml
compose:
  files: [tests/docker-compose.test.yml]
  project_directory: tests
```

A test bind such as `../services:/app/services` can then refer to the backend's `services/` directory while staying within the worktree.

Paths may be absolute if they remain inside the resolved worktree. Missing required sources, paths escaping the worktree, `.git` sources, symlinks in synchronized sources, and special files such as sockets or FIFOs are refused. Podgrove does not create a missing bind source as an empty directory.

## Node placement

`node_mode: shared` is the default. The pod selects Linux nodes, uses required affinity to exclude `eks.amazonaws.com/compute-type` values `fargate` and `auto`, and does not add tolerations for node taints. It retains its own engine, PVC, provisioning-marker/resource ownership, NetworkPolicy, and configured resource requests/limits. Both modes check namespace-scoped permissions without reading node inventory.

`node_mode: tainted` adds the configured node selector and an exact `Equal` toleration for the configured taint. The administrator must verify that the approved pool carries compatible labels/taints and has capacity. Podgrove reports scheduling outcomes from the owned Pod; it never lists or changes nodes, and this mode adds no cluster permissions.

For an existing pool with different labels and taints:

```yaml
node_mode: tainted
tainted_nodes:
  selector:
    example.com/pool: development
  taint:
    key: example.com/workload
    value: compose
    effect: NoSchedule
```

Every field in `tainted_nodes` is optional. Omitted taint fields inherit the defaults in the table; a provided selector map replaces the default selector. Label keys and string values are validated; valid empty label or taint values can be expressed as `''`. Explicit Fargate/Auto Mode selectors and non-Linux OS selectors are refused before writes, and unknown section keys are refused. These placement settings do not define application services or modify node taints. In shared mode they do not add any selector or toleration.

Override the config for a launch or inspect a placement mode with:

```sh
podgrove up --node-mode shared
podgrove doctor --node-mode shared
podgrove doctor --node-mode tainted
```

Both node modes require cluster admission to allow the privileged engine. `doctor` checks a new Pod through a server-side dry-run, or verifies an existing compatible engine, without persisting a Pod. The target namespace must already exist and its reviewed bootstrap marker must match. `up` validates that namespaced marker before creating supporting resources; no Namespace object is read. A node-mode change cannot override a denying admission policy.

A pod's `podgrove.dev/node-mode` label records its placement mode. The privileged engine shares the node kernel in either mode; Podgrove does not change node labels, taints, or permissions. Namespace mode is independent of node mode. Existing environments keep their recorded placement: changing `node_mode` or active `tainted_nodes` settings requires recreating the environment with `down`, which removes its PVC data.

## Sync exclusions and endpoint exports

See the [README](../README.md#lifecycle-and-target-selection) for `podgrove env`, retained-container logs and endpoint status. Optional `sync.exclude: [__pycache__, "*.pyc", .pytest_cache]` excludes local generated content before mirror scanning. Slashless globs match any component; slash globs are workspace-relative and `**` spans directories. Excluded directories prune descendants. Explicit mounted/config/secret sources may not themselves be excluded. Existing mirrored exclusions are left untouched. Compose build `.dockerignore` and native watch rules remain independent. Invalid patterns, including absolute paths, negation or `..`, are refused.

## Resource sizes

When no `resources` block is present, the selected `size` supplies this engine preset:

| Size | CPU request / limit | Memory request / limit |
| --- | --- | --- |
| `small` | `250m` / `2` | `2Gi` / `2Gi` |
| `medium` | `1` / `4` | `8Gi` / `8Gi` |
| `large` | `2` / `8` | `16Gi` / `16Gi` |

Presets also supply an engine `ephemeral-storage` limit of `4Gi` and no explicit ephemeral-storage request. Preset memory requests equal limits. If eligible nodes lack allocatable capacity, the Pod remains pending while the cluster's existing autoscaler may provision a suitable node; Podgrove does not change node pools or reduce the reservation.

### Explicit engine and initializer resources

Each worktree loads its own `podgrove.yml`, so worktrees in the same namespace can request different engine resources and PVC capacities. Use `resources` for custom engine sizing. Use `init_resources` independently for the storage initializer. Both accept only `requests` and `limits`, each with any subset of `cpu`, `memory` and `ephemeral-storage`:

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
init_resources:
  requests:
    cpu: "20m"
    memory: 32Mi
  limits:
    cpu: "200m"
    memory: 64Mi
storage:
  size: 40Gi
```

Each explicit block **replaces the entire corresponding resource specification**. It is not merged with preset/default values. For example, `resources: {requests: {memory: 6Gi}}` sends only that memory request: no CPU request or engine limits are added by Podgrove. `resources: {}` sends no engine requests/limits; `init_resources: {}` does the same for the initializer. Omit an unwanted dimension rather than setting it to `null`. Empty `requests` or `limits` maps are also accepted. Explicit zero is preserved: `requests: {cpu: 0}` reserves zero CPU instead of inheriting a positive CPU limit as its request. Zero limits are accepted by the API; CPU/memory zero limits do not impose a finite container cap. Positive requests cannot exceed a declared zero limit. Cluster policy can still reject zero quantities. PVC capacity must remain strictly positive.

If `resources` is absent, YAML `size` selects the preset, defaulting to `medium`. If both are present, `resources` wins and YAML `size` is ignored until the resources block is removed. An explicitly supplied `up --size small` selects that entire preset even when YAML contains custom resources; it does not alter `init_resources`. Leaving `init_resources` absent retains its historical requests of CPU `10m` / memory `16Mi`, limits of CPU `100m` / memory `32Mi`, and no ephemeral-storage values. `resources` never changes these initializer defaults implicitly.

Resource values must be nonnegative Kubernetes quantities within the platform's representable range. Quoted values are recommended, for example `cpu: "1.5"`, `cpu: "1500m"` or `memory: "6Gi"`; numeric YAML quantities are also accepted. CPU precision cannot be finer than `1m` (`0.001` CPU). For each supplied request/limit pair, the request must be no greater than the limit. Unknown resource names, unsupported section keys, non-finite/negative quantities and invalid units are rejected locally. There is no Podgrove-specific maximum inherited from `large` or any other preset.

Namespace LimitRanges, quotas and admission policies still apply to custom or empty blocks. Kubernetes may default omitted values: a limit without a request becomes the request when no admission-time default supplies one. Consequently, a requests-only or empty block does not guarantee an uncapped admitted container on every cluster. The dashboard's Configuration → Resources tab compares the current file with observed allocations, including initializer resources, ephemeral storage and PVC capacity. These values are allocations, not live usage measurements. [Kubernetes resource defaults](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/#requests-and-limits).

The engine budget covers its Docker daemon, builds and **all nested Compose services together**. Per-service `cpus`, `mem_limit` and `deploy.resources` remain in the original Compose files; they do not allocate additional Kubernetes engines or override the engine budget. Leave room for builds and the daemon when choosing application limits. The [complete resource example](../examples/resources/podgrove.yml) pairs custom Kubernetes allocation with [a Compose service](../examples/resources/compose.yml) that has its own application limits.

### PVC capacity and compatibility

`storage.size` saves the engine PVC request in YAML, defaulting to `20Gi` when omitted. `up --storage 40Gi` overrides it for that invocation. Save the desired capacity in YAML so future `up`, reconnect and refresh commands use it without repeating a flag. A one-off CLI value is not written back to configuration; later commands must still request the same compatible capacity. PVC capacity uses the same Kubernetes quantity syntax as memory and ephemeral storage, but must be strictly greater than zero; engine and initializer resource quantities may be zero. Podgrove imposes no additional capacity ceiling.

`cluster.storage_class` supplies an administrator-approved startup class; `up --storage-class NAME` overrides it. A new PVC requires an explicit class; reconnect can reuse the class on the existing owned PVC. Podgrove checks PVC identity, access mode, size, class compatibility and deletion state, but never reads StorageClasses, PVs or cloud volumes. The administrator verifies provisioning/reclaim behavior independently. PVC deletion is not proof of backing-volume deletion. The worktree mirror, Docker data and Compose named volumes use the PVC; engine ephemeral storage is a separate container allocation.

Inspect the desired requests with `up --dry-run --json` and check new-Pod admission with `doctor`. Resource and storage configuration reaches both paths. For an existing environment, incompatible engine/initializer resources or PVC capacity/class are refused **before stopping the active session**. Reconnect and `up --refresh` retain compatible existing Pods/PVCs; neither performs live resizing or silently recreates an engine. Restore the previous values to reconnect, or plan a separate environment and data migration for a new allocation. `down` removes the PVC and database contents, so do not use it as a routine sizing-update command.

## Compose and forwarding

The CLI executes the original Compose files with their original overlay order. It does not generate a Kubernetes representation of the services, choose a different Compose project name, or edit the Compose files. Network names, aliases, named volumes, profiles, dependency conditions, restart policies, healthchecks, and supported build options are evaluated by Docker Compose itself.

A forwarded target must match exactly one explicit, published TCP port on one active service replica. For a mapping `8080:8000`, use `port: 8000`; Podgrove tunnels to the daemon's `8080` and prints a selected laptop port. Random daemon ports, published ranges, ambiguous multiple publications, and forwarded scaled services are refused. UDP cannot be forwarded. A service's `expose` entry alone does not publish it.

Omitting `forward` selects all published ports. An explicit list restricts the laptop's listeners; it does not change the ports published inside the isolated engine. Each listener binds only to `127.0.0.1`. Printed `http://` URLs are convenience addresses: a Redis, MongoDB, TLS, or other TCP service still needs its appropriate client and protocol.

Application ports use `kubectl port-forward`. The Docker API uses a separate loopback proxy over `kubectl exec -i` and the engine's `docker system dial-stdio` Unix-socket connection. This preserves output after stdin EOF for exec, bind sync, and Compose watch hooks. New Pods expose their UID through the downward API; each connection checks it and full ownership is revalidated every 30 seconds. Legacy Pods without that binding use full checks per connection. These are transport details, not additional YAML fields.

Bind/config/secret sources are mirrored through one persistent owned helper exec stream, preserving numeric ownership, modes, and existing file-bind inodes. Startup prepares this connection even when a reconnect has no changed files. Each batch is length-framed and acknowledged after apply; the local baseline advances only on its matching acknowledgement. Uncertain acknowledgement fails without blind replay; explicit `up` resumes from the committed baseline. Compose watch keeps its own `CopyToContainer` sync and declared actions. Neither path uses `kubectl cp`. See [file-sync architecture](how-it-works.md#storage-and-file-sync).

## CLI overrides

Options follow the subcommand:

```sh
podgrove up --context cluster-name --namespace my-development --project-directory /path/to/worktree \
  -f compose.yml -f compose.test.yml --size large --storage 30Gi --timeout 900
```

Repeated `-f` replaces the YAML's `compose.files` list; its order is preserved. `--size` overrides the YAML size, and `--node-mode` overrides `node_mode`. `--config PATH` selects another configuration file inside the worktree. `--timeout` defaults to 600 seconds for individual startup stages; total `up` waiting time can be longer because engine readiness, building, and service readiness are separate stages.

Use `podgrove validate` for local validation or `podgrove up --dry-run` for generated manifests without cluster writes. They require a namespace from YAML or a flag but no Kubernetes context, because they do not access the cluster. Other public lifecycle commands require an explicit context from `--context`, `cluster.context`, or the `PODGROVE_CONTEXT` fallback.

A configured namespace or namespace base accepts a Kubernetes DNS-label name. Podgrove never reads, creates, adopts, relabels or deletes Namespace objects. Worktree mode checks the exact identity in the bootstrap ConfigMap; shared mode requires a shared marker without a worktree identity. Environment resource deletion requires both Podgrove management and exact environment labels. An environment cannot adopt existing resources belonging to another owner.


## Lifecycle and environment variables

| Variable | Purpose |
| --- | --- |
| `PODGROVE_CONTEXT` | Explicit Kubernetes context fallback when neither `--context` nor `cluster.context` is supplied. |
| `PODGROVE_STATE_HOME` | Override private local state storage; default `~/.local/state/podgrove`. Keep it outside synchronized worktree directories. |
| `PODGROVE_GITLAB_TOKEN` | Optional token for reading a private GitLab merge request's state. It is used only for the lifecycle API request. |
| `PODGROVE_REPO`, `PODGROVE_OWNER`, `PODGROVE_BRANCH` | Explicit resource-label overrides. Repository/branch defaults come from bounded local `.git`/`commondir`/`HEAD` metadata; absent or unsupported metadata falls back to the root name and `unspecified`. Owner defaults to local `USER` or `unknown`. No Git commands run. |

Repository labels use the common repository directory when available, so linked worktrees can share a repository label while keeping separate environment identities. Branch labels use the symbolic local HEAD or `detached-<12-hex-prefix>` for a detached commit. Podgrove reads neither remote URLs nor Git config, hooks, objects or indexes, and it modifies no Git metadata. Values are sanitized to Kubernetes label syntax. These labels describe creation-time checkout metadata; a later checkout or rename does not relabel an existing environment or force it to be recreated.

The dashboard reads current branch and repository metadata again when refreshed, without applying environment-variable label overrides. It can therefore show a later branch change while the environment's creation-time labels remain unchanged. Missing branch metadata is reported as unavailable.

Local state and session logs are stored by worktree identity and cluster context, with private directory/file permissions. Session control uses a private Unix socket and a per-session token. State contains resolved paths and connection metadata; logs can contain application output. Keep needed diagnostics before `down`: after successful cluster cleanup, it removes the matching local state, log, recognized temporary files, socket, and released lock. Failed cleanup retains the state binding for retry. Other environments remain; an empty state directory is removed.

List this machine's known environments without entering a worktree:

```sh
podgrove status --context cluster-name --namespace my-development --all --json
podgrove status --context cluster-name --namespace default --all
```

This reads validated local records and checks session connectivity. It does not discover all cluster environments or ask every Compose stack for fresh service health. Invalid or foreign local records are reported as errors. For a specific worktree's service health, use ordinary `status`.

The connected session checks cleanup conditions on activity and at least every 30 seconds, with shorter intervals for short TTLs. Local bind/config/secret changes, edits beneath declared Compose watch paths, and successful `status`, `logs`, or `exec` commands refresh activity. Browser traffic and application requests do not refresh the idle clock. Set an appropriate TTL for unattended test runs.

To associate a GitLab merge request:

```sh
podgrove up --mr-url https://gitlab.com/group/project/-/merge_requests/123
```

Only HTTPS merge-request URLs on `gitlab.com` are accepted. A verified `merged` or `closed` state triggers cleanup. API failures retain the environment; they do not imply a closed MR. The optional token needs only the access required to read that project's MR state. Local advisory branch labels never discover or associate a merge request; its URL is explicit, and no Git command is performed.

A disconnected laptop does not run a cluster controller. Arrange a separate reaper process or scheduler if cleanup must continue while the laptop is unavailable:

```sh
podgrove reap --context cluster-name --namespace wt-0123456789ab --dry-run
podgrove reap --context cluster-name --namespace wt-0123456789ab --environment 0123456789ab --dry-run
podgrove reap --context cluster-name --namespace wt-0123456789ab --all --watch --interval 60
```

Supply the actual namespace or use the worktree's YAML. By default, `reap` evaluates only the current worktree identity. `--all` explicitly scans eligible environments within that one namespace; it cannot be combined with `--environment`. `--environment` accepts a 12-character lowercase hexadecimal identity; it does not bypass cleanup conditions. The reaper requires expired TTL, a verified merged/closed MR, or proof of local worktree removal. It checks ownership, recorded namespace mode, and the current lease before deletion. Namespaces and bootstrap resources remain intact in all modes, including legacy exclusive records. There is no all-namespace mode. Reaping removes owned PVC data just as `down` does.

Worktree removal is recognized only from a matching validated private record on the machine running the reaper: the recorded root must be absent while its immediate parent remains an ordinary directory. A missing parent/mount is insufficient, and a root replaced by a non-directory is refused. The reaper does not use cluster-supplied filesystem paths as local evidence. A matching local session is stopped through authenticated control and its files are cleaned after cluster removal. A remote scheduled reaper cannot infer another laptop's deletion or remove that laptop's files; TTL/MR checks remain available there.

If local state was lost, `down --project-directory /original/worktree --namespace <known-namespace>` can recover the deterministic environment identity and target that explicit namespace. It does not search other shared namespaces. Preserve state until cleanup succeeds whenever possible.
