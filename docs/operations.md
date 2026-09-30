# Operate worktree environments

This guide covers an environment after [initial setup](getting-started.md) and [Compose adoption](compose-migration.md). Run commands with your installed `PODGROVE_BIN` from the application worktree; keep the target in `podgrove.yml`.

## Endpoints and live edits

By default, Podgrove forwards every published TCP port to `127.0.0.1`. Target-only declarations such as `ports: ["27017"]`, zero-valued publishers, and published ranges use Docker’s observed allocation after startup. Each forwarded target must resolve to exactly one publisher on one active replica. `expose` alone creates no endpoint. If Docker changes or removes a publisher later, Podgrove disconnects those application forwards and asks for `up --refresh`; it never keeps forwarding to a stale mapping. For a Compose mapping `8080:8000`, a forwarding entry uses the **container target `8000`**:

```yaml
forward:
  - service: api
    port: 8000
```

This fragment assumes `api` exists and publishes that target. Omit `forward` for automatic selection, supply a list to restrict it, or use `forward: []` to disable application tunnels. Optional `local: 43123` fixes a laptop port; normally omit it so concurrent worktrees can choose available ports. An occupied explicit port is an error. Automatic ports prefer a stable worktree-specific number and choose another if needed. Printed `http://` addresses are conveniences; Redis, MongoDB, TLS, and other TCP services still need their own client/protocol.

Bind/config/secret files are copied before service startup, then local changes are mirrored one way. Remote edits do not sync back. Image-only source needs a declared Compose `develop.watch` action or an explicit `up --refresh`; Podgrove does not invent reload rules. A declared watch rebuild action can rebuild an image. Build contexts still follow the project's `.dockerignore`. See [file-sync architecture](architecture.md#storage-and-file-sync).

To exclude local generated files from the mirror, opt in explicitly:

```yaml
sync:
  exclude: [__pycache__, "*.pyc", .pytest_cache]
```

Slashless patterns match any path component; slash-containing patterns are relative to the selected workspace root, with `**` matching nested directories. An excluded directory excludes its descendants. Absolute paths, `..` and negation are refused. An explicitly mounted/config/secret source cannot itself be excluded. Exclusions do not change Compose build contexts or native watch rules, and do not delete already mirrored or remotely generated files. There are no implicit `.gitignore` rules; `.git` remains forbidden. Add dependency/cache directories only when your containers do not need their local contents.

Files changing while a local snapshot is prepared are retried with backoff without shutting down the application forwards. An idle sync connection can recover with at most three attempts: each checks the original engine's ownership, restarts only its verified sync helper, and confirms that the remote baseline matches the last acknowledged batch. Recovery does not rebuild or restart application services. An uncertain transfer, changed baseline, or exhausted recovery pauses sync while healthy forwards and session commands remain available. `status --json` reports `sync_status` separately; the dashboard shows it in the Engine tab. Inspect the diagnostic and remote mirror before running `up --refresh` to resume a paused sync. A failed or uncertain remote transfer is not replayed automatically.

Forwarding has an independent monitor that restarts a dropped process/listener on the **same local ports**, verifies the original engine's ownership and UIDs, and reports `reconnecting` while retrying. Three failed retries leave endpoints `disconnected` and the session `degraded`; run `up` to reconnect. Pod replacement or ownership changes are never silently adopted. A listening forward proves local tunnel availability, not application health. A failed ownership read marks forwarding `reconnecting` while keeping the existing verified listener during a bounded 120-second grace period. Proof expiry closes it; only a fresh matching UID check can authorize reconnection. Lease heartbeat failures are reported separately and retried from a fresh ownership read with capped backoff.

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

Calling ordinary `up` again returns an unchanged healthy session when no bind sources or file-backed configs/secrets need mirroring. With those sources, it remirrors before reconciling Compose; exited or unhealthy services are recreated so a newly added required file can fix the first boot. A partial startup also triggers reconciliation on the next `up`. Reconciliation restarts the local session and can briefly interrupt forwards. Independently healthy services keep their container identities unless Compose detects a configuration change. Fingerprints use deterministic serialization; the earlier unversioned format is also recognized when omitted network/sync settings still have their original defaults. Changed configuration re-runs Compose against the same engine/PVC. Use `--refresh` for build-only source or service env-file changes that do not change that model. Run `up` again after a disconnected session. Existing engines/PVCs retain their size and placement; flags do not resize them. Recreating with `down` destroys their data.

Status includes the worktree identity/root and each forward's state, separately from Compose service health. `engine_identity` records captured and observed Pod/StatefulSet UIDs, so replacement remains visible even when the new Pod reports zero restarts. During startup, a verified Pod replacement can retry build/start at most twice within the startup deadline, after the replacement is ready and the original controller specification and PVC UIDs are rechecked. `startup_status` records retry attempts. Controller/PVC changes, unverifiable ownership, or exhaustion stop recovery; ordinary `exec` commands are never replayed. Transient Docker status reads receive three bounded attempts. Exhaustion reports valid JSON with degraded/stale health and keeps the session and forwards available for a later observation. `down --json` returns a JSON cleanup result; cleanup errors return a JSON error and preserve the state needed to retry. `env` exports only ready endpoints as `PODGROVE_<SERVICE>_<TARGET_PORT>_HOST`, `_PORT` and `_URL`; `--json` returns a JSON mapping. Shell output contains quoted `export` statements. `_URL` uses HTTP as a convenience; build MongoDB/Redis/TLS connection strings using your application's protocol and test credentials. Normalized service-name collisions are refused; `status --json` retains original service names.

`doctor --json` writes one JSON document to stdout: `command: doctor`, `status: ok`, the resolved target, check results and administrator-verification notes. Configuration or check failures instead return `status: error` with an `error` message and exit 1; interruption returns the same error envelope with exit 130. Without `--json`, doctor retains its human-readable output. Successful checks do not prove node capacity, StorageClass reclaim behavior or backing-volume deletion.

A Compose build or service-start failure after a successful mirror keeps the session, sync and forwards for observed running services available. A running container can still be unhealthy; forwarding readiness does not imply application health, and exited/absent services receive no forward. `up` returns nonzero with `startup_status.state: failed`; use `exec`, `logs` and `status` to diagnose, then rerun `up` after fixing the source. If an earlier infrastructure failure disconnected the session, `logs SERVICE` opens a temporary, ownership-checked Docker connection to the retained engine. Newly recorded environments retain the original Compose project/service names, so logs still work when the Compose sources have since changed or disappeared; pass explicit context and namespace if YAML itself is invalid. This diagnostic command neither rebuilds nor restarts containers, and closes the temporary connection on exit. The engine must still exist. Older records without project metadata require their original valid Compose configuration.

During startup, the session log immediately records the current phase before waiting for the engine, Docker connection, initial file copy, Compose build/readiness and application forwards. `status` supplies the log path while the environment is starting. These bounded phase messages contain no configuration or command arguments; a quiet phase does not establish a deadlock. Compose build output remains captured until that command finishes.

`exec` streams stdin, stdout and stderr through an owned Kubernetes exec using WebSockets. Non-interactive exports verify separate stdout/stderr checksums and acknowledge receipt before the remote wrapper exits, so a successful transport exit alone cannot hide truncated output. Redirecting either output stream selects this binary-safe mode; fully interactive terminals keep their native terminal and resize behavior. Both paths select the exact Compose project/service container, verify the engine and PVC identities, and preserve the remote exit status when execution completes. An interrupted or unverifiable command is never replayed; inspect its effects before retrying.

Existing environments created by an older version from a Git subdirectory keep their original identity. The new resolver refuses to silently adopt or merge those records: use the recorded version and original directory to inspect or retire that environment before starting from the worktree root. Keep the previous versioned installation available during upgrades.

Target precedence is per field: an explicit CLI flag **after the subcommand**, then YAML, then `PODGROVE_CONTEXT` for context only. Namespace has no implicit fallback. An explicit `--namespace` is an exact target and selects shared mode unless accompanied by `--namespace-mode worktree`; this allows recovery using an already-derived namespace without deriving it twice. The current kube context is never an implicit target; Podgrove does not select or modify Docker contexts.

Always use the intended context. Local records are keyed by worktree and context, so changing `cluster.context` selects a different record. Within the same resolved context, existing `status`, `logs`, `exec`, and `down` retain the recorded namespace; changing YAML namespace cannot redirect those operations. A mismatching explicit `--namespace` is refused. If the default YAML becomes invalid, explicit `--context` and `--namespace` together allow target-only commands such as `down` to bypass it; an explicit `--config` is still validated.

`status --all` lists this machine's saved environments for the resolved context and namespace. It needs no application worktree when its exact target is supplied explicitly. It does not discover other machines' environments or query every stack's current service health; use ordinary `status` for that worktree.

## Engine disruption protection

Every engine Pod carries `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"` and `autoscaling.cast.ai/removal-disabled: "true"`. Its namespaced PodDisruptionBudget selects only that engine and sets `maxUnavailable: 0`. `up` repairs missing protections on an existing owned engine without rolling the Pod; `doctor` reports missing protections. Upgrade the namespace RBAC bundle first.

These controls discourage supported autoscaler removals and voluntary evictions. They cannot prevent forced deletion, node failure or every spot interruption. Optional [placement settings](configuration.md#node-placement) can select an existing on-demand pool without changing nodes. See the upstream [Kubernetes PDB guidance](https://kubernetes.io/docs/tasks/run-application/configure-pdb/) and [CAST AI preparation guidance](https://docs.cast.ai/docs/preparation).

## Worktree network isolation

Generated `05-network-isolation.yaml` denies ingress and egress for **Pods labelled `app.kubernetes.io/managed-by: podgrove` only**. Unrelated or unlabelled CI Pods are not selected. Each worktree then gets a separate policy: no incoming Pod traffic, DNS only to `kube-system` Pods labelled `k8s-app: kube-dns` on TCP/UDP 53, and public IPv4 TCP 80/443 for image pulls and build dependencies. It adds no general same-namespace or other-namespace access. Explicit [`connect` declarations](connectivity.md) add only the chosen source/target engine and published TCP port; `reverse` reaches a declared laptop loopback port over authenticated exec. Services within one Compose stack communicate inside that worktree's Docker engine; CLI tunnels use authenticated Kubernetes exec/port-forward.

Public-web egress excludes private, shared-address, link-local/metadata, loopback, documentation, benchmark, multicast and reserved IPv4 ranges. **Before deployment, the administrator must review the actual Pod, Service, node and control-plane ranges**, and add any additional infrastructure/public ingress addresses that must be unreachable. Save those values in each worktree's `podgrove.yml`:

```yaml
network:
  blocked_cidrs:
    - 203.0.113.0/24 # Documentation example; replace with actual infrastructure CIDRs.
```

These exclusions supplement the built-in ranges; they never replace them. They restrict the public-web rule, while the narrow DNS exception remains. Use CIDR network addresses without host bits. IPv6 internet egress is not enabled; matching cluster DNS can still use IPv6. If the environment needs no public web access, `blocked_cidrs: ["0.0.0.0/0"]` leaves only the scoped DNS allowance, which also prevents remote image pulls/build downloads from the engine. [Complete example](../examples/network/podgrove.yml).

`up` reconciles the engine's owned policy, including when the session is already running; changed network configuration is part of the startup configuration. `down` removes that engine's policy while retaining the administrator-installed deny policy for Podgrove-managed Pods. Existing installations need a newly generated bootstrap folder reviewed/applied to receive the baseline. Optional scheduled reapers need the separate reviewed API/DNS policy in [their templates](../deploy/README.md#optional-scheduled-reaper).

These rules need a CNI that enforces NetworkPolicy. Kubernetes allow rules are additive: another broad allow policy can reopen traffic. Node-local traffic, Service address translation and public ingress/proxy paths also require platform review; public HTTP(S) access is not an absolute ban on every public endpoint backed by another namespace. The generated developer permissions can manage namespace policies, and privileged engines share their node kernel. For a stronger boundary, administrators must enforce network rules outside that developer identity using their CNI/admin policy or an approved egress gateway. Validate actual packet paths before treating isolation as established. [Kubernetes policy behavior](https://kubernetes.io/docs/concepts/services-networking/network-policies/).

## Browser dashboard

From an application worktree containing the target configuration:

```sh
"$PODGROVE_BIN" web
```

To develop the dashboard from this checkout, supply an application project directory with `--project-directory`; no developer cluster settings are shipped.

No context or namespace flags are needed when YAML supplies them. A dashboard-only directory needs no Compose file; use the [dashboard-only example](../examples/dashboard/podgrove.yml) and set its approved target. Distributed packages do not include a developer target configuration. Optional overrides:

```sh
"$PODGROVE_BIN" web --port 8765 --no-open
"$PODGROVE_BIN" web --project-directory /path/to/application-worktree --config podgrove.yml
```

The dashboard binds only to loopback, chooses an available port by default, opens a browser, and stays in the foreground until Ctrl-C. It needs no frontend build. It lists this machine's recorded worktrees for the resolved context/namespace, with branch/worktree identity, Compose services, engine/Pod allocations, claimed PVC capacity and searchable endpoints. The Logs tab offers snapshots or a live connection, pause/resume, wrapping, copying and fullscreen. Select **All logs** to request all retained history for the selected source; snapshots have a 64 KiB limit and report clipping. Live connections last up to five minutes; resuming starts a fresh tail and can repeat or miss lines. Displayed history is bounded to 2,000 lines and 256 KiB, with a visible notice when older lines are trimmed. Export retained service history without these dashboard display limits using `podgrove logs SERVICE --tail all > service.log` (add `--follow` to continue streaming). CLI output comes directly from Compose and is not redacted by the dashboard; already rotated logs cannot be recovered. Allocations are not live resource-usage measurements.

The sidebar's Configuration page separates cluster, access, worktree and resource settings into tabs. It shows safe current YAML settings, saved network exclusions, observed engine/initializer allocations and PVC capacity, the selected context/namespace, provisioning-marker metadata, and scoped observed ServiceAccount/RBAC declarations. It does not read Namespace or cluster-RBAC metadata. These declarations do not prove effective permissions or that bootstrap was installed. Viewing/refreshing does not extend TTL or change environments; saved health is labelled as a snapshot. Common credential patterns in logs are redacted, but arbitrary log secrets may still appear. See the [dashboard guide](web.md).

## Node placement and resource sizes

`node_mode: shared` schedules on eligible existing Linux nodes, excludes Fargate and EKS Auto Mode, and adds no tolerations for existing taints. Neither mode reads node inventory. Optional `node_mode: tainted` selects an existing pool using `tainted_nodes.selector` plus a matching taint key/value/effect. The platform owner supplies compatible selector/taint values; Podgrove reports scheduling outcomes through its owned Pod status. See [node placement](configuration.md#node-placement) for a complete example.

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

The optional `init_resources` block independently replaces resources for the storage initializer using the same rules, including `init_resources: {}`. If omitted, it retains requests of `10m` CPU / `16Mi` memory and limits of `100m` CPU / `32Mi` memory. See the [complete example](../examples/resources/podgrove.yml) and [configuration reference](configuration.md#resource-sizes) for all fields and precedence.

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

The reaper requires a namespace from YAML or `--namespace` and never scans arbitrary namespaces. **By default it considers only the current worktree identity.** Use `--environment <12-character-identity>` for another exact environment, or explicit `--all` for every Podgrove environment in the selected namespace. These flags do not force deletion before cleanup conditions are met. Multiple seats sharing one OS user also share the default local state directory; use separate worktree roots and, if desired, `PODGROVE_STATE_HOME` directories. Namespace-wide `--all` remains broad even with separate local state. Removed-worktree cleanup requires matching private state on the reaper's machine and an existing parent directory. A remote reaper cannot infer another laptop's filesystem state. Reaping is not restricted to disconnected sessions: expired connected stacks can also be removed. See [lifecycle details](configuration.md#lifecycle-and-environment-variables) and the separately reviewed [scheduled reaper](../deploy/README.md).

For two-repository backend/web lanes, stable Compose interpolation, endpoint exports and a test procedure for your approved cluster, read [development workflows](dogfood-workflows.md).
