# How Podgrove works

Podgrove runs the laptop's Docker Compose client against a worktree-specific Docker daemon inside Kubernetes. Compose remains responsible for the application: Podgrove creates the engine, mirrors local files needed by bind mounts, and maintains the connection.

```mermaid
flowchart LR
    subgraph Laptop
        Files[Worktree and unchanged Compose files]
        CLI[Podgrove background session]
        Compose[Docker Compose client]
        Proxy[Loopback Docker API proxy]
        Client[Browser or test runner]
        Files --> CLI
        Files --> Compose
    end
    subgraph "Owned Kubernetes environment"
        API[Docker Unix socket]
        Engine[Privileged Docker engine]
        Stack[Compose services and networks]
        PVC[(PVC: Docker data and worktree mirror)]
        API --> Engine
        Engine --> Stack
        Engine --> PVC
    end
    CLI --> Proxy
    Compose --> Proxy
    Proxy -->|kubectl exec -i and dial-stdio| API
    Client -->|kubectl port-forward| Stack
```

## Startup

1. Resolve the worktree and parse the version 1 config. Cluster operations require an explicit context and namespace from `cluster.context`/`cluster.namespace` or their command flags; no namespace is selected implicitly. Ask `docker compose config` to normalize the original files, then validate unsupported host-dependent features, local source paths, and requested port mappings before cluster mutation.
2. Derive a stable environment identity from the nearest Git checkout’s top-level path, including linked worktrees. Retain the configuration/Compose directory separately; nested invocation paths share the same identity. Outside Git, use the selected directory. `cluster.namespace_mode: shared` uses the configured namespace exactly; `worktree` derives `<base>-wt-<identity>` with shortening/hash for long bases. Check namespace-scoped access and the approved node-placement configuration without reading nodes. Validate any existing owned PVC's identity/spec and require an administrator-approved class for a new PVC. No Namespace, StorageClass or PV reads occur; reclaim behavior remains a platform responsibility.
3. Validate the exact namespaced `podgrove-bootstrap` ConfigMap, including mode and worktree identity where applicable. Its namespace must have been prepared by the platform; Podgrove never creates, adopts, labels or deletes it. Server-side dry-run the engine Pod before creating supporting resources. After admission succeeds, create the owned NetworkPolicy, PVC, lifecycle ConfigMap, headless Service and single-replica StatefulSet. An existing compatible owned engine can be reused without a new Pod CREATE.
4. Wait for the Docker pod and verify its StatefulSet, labels, Pod UID, and PVC claim. Start a loopback Docker API proxy using Kubernetes exec and `docker system dial-stdio`, then establish a clean `DOCKER_HOST` environment without inherited Docker-context or TLS overrides.
5. Mirror every bind-mount source and local file-backed Compose config or secret before calling `docker compose up --detach --build`. The remote absolute source path equals the path that Compose resolved locally.
6. Wait for service readiness, including successful dependency jobs. Start application tunnels and, if declared, `docker compose watch --no-up`. Return the local endpoints while the supervisor remains in the background.

No generated Compose file replaces the original configuration. Builds use the Docker client's normal context upload. Podgrove does not rewrite service names, commands, networks, volumes, or dependency graphs.

Before startup, `podgrove bootstrap --output <new-folder>` renders five YAML files with nine namespaced setup objects. The administrator reviews and applies the bundle to the existing actual namespace. The marker, managed-Pod deny policy, client/reaper ServiceAccounts and Role/RoleBindings grant no cluster access. No Namespace, quota, LimitRange or admission policy is emitted. Generation cannot inspect existing CI workloads or name collisions; its baseline leaves unrelated/unlabelled Pods unselected. Shared mode installs once per namespace; worktree mode needs a prepared namespace and marker for each exact worktree identity. The dashboard reads that marker and seven known namespaced SA/RBAC declarations, never Namespace or cluster RBAC metadata.
## Storage and file sync

A single-replica [StatefulSet](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/) named `pg-<identity>` manages the engine pod `pg-<identity>-0`. It recreates a deleted pod using the same independent PVC. `OnDelete` updates and compatibility checks prevent reconnects from silently rolling an engine. Podgrove verifies the real controller UID in the pod owner reference before using it.

The one PVC has two subdirectories: `docker` is mounted at `/var/lib/docker`, and `worktree` is mounted at the absolute local worktree path inside the Docker pod. Both named Docker volumes and mirrored bind data therefore survive a pod restart. These are PVC mounts inside the pod, not node `hostPath` mounts.

The synchronizer starts a small `alpine:3.21` helper on the worktree's Docker engine. It mounts only that daemon's worktree path at `/workspace`, with a separate owned Docker volume for sync metadata. On first use it refuses to overwrite a nonempty mirror without matching ownership state. It does not create a marker file inside the mirrored worktree.

Initial and incremental transfers share one persistent, owned `docker exec -i` connection to the helper. Each tar batch carries a fixed-size header with a connection nonce, sequence number and exact byte length. The receiver spools the complete declared frame before extracting into a private stage and applying it; a truncated header, body or archive fails before mirror application. Only one batch can be outstanding. Startup completes the helper handshake even when a reconnect has no changed files, so the first later edit reuses an already prepared stream. This avoids opening another Docker exec connection for every edit.

The helper commits its persisted baseline after applying the batch, then acknowledges that connection nonce and sequence. The laptop advances its own baseline only after receiving that exact acknowledgement. A lost or invalid acknowledgement makes the connection unusable: the session fails without blindly replaying a possibly committed batch. Run `podgrove up` to reconnect; startup verifies ownership, replaces the old helper and reloads the committed remote baseline. Unchanged local files and remote-only content are retained.

The stream uses the same UID-guarded Docker API transport as Compose, with periodic ownership checks that revoke active streams on drift. Chunked I/O and bounded acknowledgement/stderr buffers limit memory; handshake and transfer waits have deadlines. Cancellation stops the owned local transport child, joins the sync worker, then removes the helper, so an uncertain remote apply cannot overlap a replacement receiver. Sync runs independently of slower service-health and cluster-heartbeat work.

Changed regular files are written **in place**, preserving the inode used by an existing single-file bind mount. The worker polls with a roughly 0.4-second idle interval; scanning and transfer time add to that interval. File content is hashed when metadata changes. No `kubectl cp` is used. A batch is not a transaction across all application files: a later application error can leave earlier writes visible while retaining the previous committed baseline for recovery.

New files, edits, numeric UID/GID ownership, mode changes, renames, and tracked deletions are transferred. Ownership IDs are preserved numerically, allowing a Compose service running as the same UID/GID to write its bind directory; account names are not translated between the laptop and container. Deleting a local directory removes its tracked remote entries; independently generated remote files are preserved, so a nonempty remote directory can remain. A persisted baseline lets reconnects retain remote contents when the corresponding local source has not changed. This is a one-way mirror, not bidirectional reconciliation; remote edits are never copied back to the laptop.

Only requested source trees are scanned. `.git` entries are always skipped by this synchronizer, including worktree pointer files. Required missing sources, symlinks, special files, and paths outside the worktree are refused. File names with spaces, Unicode, newlines, or shell metacharacters are handled as data.

Compose `develop.watch` is a separate mechanism. Its own rules and actions run against the same remote engine: Compose transfers file content through Docker's `CopyToContainer` API and runs declared exec hooks through the engine's exec API. A `sync` rule can update an image's filesystem without a bind mount or rebuild. Declared `rebuild`, restart, and `sync+exec` actions retain their normal meaning. Applications still need their own reload support to react to changed files.

## Docker API transport

Every local API connection receives a separate binary stream through `kubectl exec -i` into the engine container. Docker's `system dial-stdio` helper connects that stream to `unix:///var/run/docker.sock`. Closing the client's input closes only the input direction; output continues until the daemon finishes. This matters for commands that produce output after stdin EOF. Ordinary Kubernetes port-forward previously cut those responses short on this cluster, sometimes leaving Docker reporting exit 0 before a command completed.

New engine Pods expose their actual UID through the downward-API `PODGROVE_POD_UID` environment variable. After initial full ownership validation, each exec compares that value with the captured UID before accessing Docker. StatefulSet/Pod ownership and PVC-claim binding checks run every 30 seconds in the background; a stale verification also forces a check before another connection. A replacement UID fails immediately inside the exec, and confirmed ownership drift fails the session at revalidation. An unavailable API read gates new Docker requests and retries with backoff capped at 30 seconds. Existing streams can continue only while the last complete ownership proof is at most 120 seconds old; expired streams reset without replay. Status exposes verification age and state. Both API and direct command transports explicitly select WebSockets. Pods without the verified downward-API binding retain full API ownership reads for each connection.

The proxy bounds concurrent connections and buffers, passes Docker bytes unchanged, and reaps its exec children on shutdown. Transport errors reset the affected connection instead of presenting a successful output EOF. Local Compose, builds, watch hooks, and bind synchronization use this API route. A failed individual transport resets only that connection and records bounded diagnostic counters; uncertain requests are never replayed. The application tunnels are separate.

User `podgrove exec` commands instead use a direct, UID-guarded Kubernetes WebSocket exec into the exact running Compose container. This avoids nesting Docker’s hijacked HTTP exec stream inside another transport. Discovery checks exact project/service/replica labels; the engine Pod, StatefulSet and PVC identities are verified before, during and after execution. Command input/output streams directly without accumulating an export in memory. Interrupted commands return a nonzero result and are never automatically replayed.

## Ports and isolation

Each engine can publish the same Compose ports, container names, network names, and named-volume names as another worktree because the daemons are separate. On the laptop, Podgrove selects free ports using a stable preference derived from worktree identity. It checks for existing listeners and binds tunnels only to loopback. Explicit `forward.local` collisions are errors. Target-only, zero and ranged Compose publications resolve from observed Docker publishers after services become ready. Mapping changes disconnect application forwards until an explicit refresh resolves the new publisher.

The daemon has a Unix socket and a loopback-only `127.0.0.1:2375` listener inside the pod; Podgrove's API proxy uses the Unix socket through exec. A portless headless Service supplies StatefulSet identity and does not expose Docker. No public load balancer is created. Separate `kubectl port-forward` tunnels reach the ports published by the nested engine for HTTP, WebSockets, and other TCP applications.

The default `node_mode: shared` places the pod on existing Linux EC2 nodes using required affinity to exclude Fargate and EKS Auto Mode. It adds no node-taint tolerations and does not require node-read permissions. Optional `node_mode: tainted` adds the selector and exact taint toleration configured in `tainted_nodes`, without inspecting cluster node inventory; the administrator supplies compatible pool settings. Its defaults are selector `podgrove.dev/dedicated=true` and taint `dedicated=podgrove:NoSchedule`; alternative keys, values, and `NoSchedule`/`NoExecute` effects are supported. The selected mode is recorded as `podgrove.dev/node-mode` on owned resources. Podgrove does not provision or modify nodes.

The Docker pod is privileged and shares its node's kernel. Separate engines, PVCs, network policies, provisioning-marker ownership, and resource limits isolate ordinary worktree operation; they are not a hardened boundary against malicious privileged workloads. No service-account token is mounted in the pod.

The scheduler reserves the whole engine's memory limit: `2Gi`, `8Gi`, or `16Gi` for small, medium, or large. Existing cluster autoscaling may supply capacity when a reservation does not fit current eligible nodes. CPU requests remain `250m`, `1`, or `2`, with limits `2`, `4`, or `8`. Each engine's NetworkPolicy selects both its management and environment labels, including when unrelated workloads share `default`.

Bootstrap supplies a retained deny policy selecting only Podgrove-managed Pods; unrelated and unlabelled CI Pods are unselected. Its installation is an explicit administrator action. The engine's policy denies inbound pod traffic and limits egress to selected CoreDNS Pods on TCP/UDP 53 plus public IPv4 TCP ports 80 and 443, excluding private, link-local and other reserved ranges. `network.blocked_cidrs` adds administrator-supplied infrastructure exclusions without replacing the built-in set. Before returning a running environment, `up` validates the namespaced provisioning marker and reconciles its owned policy with UID/resource-version preconditions; it does not leave a missing or weakened policy behind merely because Compose settings are unchanged.

Standard policies combine their allowances. Other allow policies, local-node exceptions, Service/NAT handling and public ingress/proxy paths require administrator review and live verification; these manifests alone do not prove a hard cross-namespace boundary. Private registries and internal dependencies need a reviewed networking design. The optional reaper has a separate labelled policy template with explicit API destination/port placeholders, rather than sharing engine web access. See [network isolation](../README.md#worktree-network-isolation).

## Supervision and lifecycle

The local session owns the engine tunnel, application tunnels, sync helper, and optional Compose watch process. A private Unix socket handles session control. An independent application-forward monitor checks the child and local listeners, verifies the captured StatefulSet/Pod UIDs, and performs three bounded same-port reconnect attempts. Exhaustion keeps sync/control alive but marks endpoints disconnected and the session degraded. A changed engine identity is never silently adopted. Captured/current Pod and StatefulSet UIDs are saved before initial sync/build; a primary failure makes a fresh guarded read to report a mid-build replacement, regardless of restart count. Confirmed replacement evidence remains latched for that session. A temporary API outage retains an existing application listener within its 120-second ownership-proof budget; slow verification reads cannot extend that deadline. Heartbeat failures retain a degraded session and retry a fresh named lease read followed by a conditional update, using backoff capped at 30 seconds. Local pre-transfer snapshot races retry with backoff; permanent filesystem errors, Compose-watch exits and uncertain transfer acknowledgements remain fatal. Transient read-only service-status failures receive bounded retries; exhaustion retains stale observations with an unavailable marker while the session stays usable. Individual Docker API transport failures do not poison unrelated requests. `podgrove up` reconnects an existing environment; failed startup resources remain available for diagnosis and explicit cleanup.

The lifecycle ConfigMap records last activity, TTL, and an optional GitLab MR URL. A connected session checks TTL/MR cleanup. A separate `podgrove reap --namespace ... --all` process can evaluate eligible environments within that namespace while sessions are unavailable. Bare `reap` targets the current worktree; `--environment <identity>` explicitly selects one other identity without bypassing lifecycle conditions. There is no automatically installed cluster-wide reaper.

The reaper also recognizes a removed worktree when the machine running it has a matching validated local state record and the old root's immediate parent still exists as an ordinary directory. It does not trust a filesystem path supplied by a cluster lease or infer deletion from an unavailable parent/mount. A reaper on a different machine can still act on TTL/MR state, but cannot infer another laptop's worktree removal. It rechecks the lease and local evidence before deletion and stops a matching local session through authenticated control.

`down` stops the local session, then deletes the owned environment. Both current namespace modes, `shared` and `worktree`, retain the preexisting namespace and administrator-installed bootstrap objects. Cleanup first deletes the owned StatefulSet with foreground cascading cleanup, then deletes only the pod, PVC, ConfigMap, NetworkPolicy, and headless Service matching both management and environment labels. `reap` uses the same recorded lifecycle mode and ownership fences. Image caches, databases, sync baselines, and mirrored files on the removed PVC are discarded. Legacy `exclusive` records also retain the namespace; no Podgrove path deletes a Namespace. New configuration cannot select that legacy mode. Namespace spelling alone does not turn a newly configured shared namespace into an exclusive one.

After successful cluster removal, `down` and a reaper with matching local state remove that environment's JSON state, session log, recognized temporary files, socket, and lock. State is deleted last so a local cleanup failure retains a binding for retry. The state directory is removed only when empty; other environments are retained. A remote reaper cannot delete files on an unavailable laptop. Underlying storage reclamation belongs to the CSI provisioner; historical administrative acceptance harnesses separately checked PV/cloud-volume removal. The current namespace-only lane proves PVC absence only; it does not read PV/cloud metadata.

`status --all` lists validated local records for the resolved explicit context and namespace without requiring a current worktree record. It probes local session connectivity and excludes private control credentials. It is a local inventory, not cluster-wide discovery or a health query against every Compose stack.

## Verification boundaries

Unit and browser tests exercise ownership, namespace resolution, generated manifests, local synchronization and forwarding failures using controlled fixtures. Opt-in Docker tests exercise unchanged Compose behavior against disposable local engines. These lanes do not establish a new cluster's admission, CSI behavior, scheduling or CNI packet paths. See [verification](verification.md) and [test lanes](../tests/README.md) for commands and boundaries.
