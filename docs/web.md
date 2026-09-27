# Read-only browser dashboard

The dashboard reads its target from the selected project's `podgrove.yml`. No developer cluster target is shipped in the checkout, wheel or source archive. Configure the [dashboard-only example](../examples/dashboard/podgrove.yml), or add an explicit target to your project's `podgrove.yml`:

```yaml
cluster:
  context: your-explicit-kube-context
  namespace: team-development
  namespace_mode: shared
```

From that project directory, run:

```sh
podgrove web
```

When developing from the Podgrove source checkout, use `.venv/bin/podgrove web --project-directory /path/to/application-worktree` to read the application's configuration.

The command chooses an available loopback port, prints its authenticated URL, opens your browser, and stays in the foreground. Stop it with Ctrl-C. Each explicit flag overrides its YAML field; `PODGROVE_CONTEXT` supplies a context only when neither flag nor YAML does. Both context and namespace must resolve explicitly: missing `cluster.namespace` requires `--namespace`, and Podgrove never selects a default namespace or current kubectl context implicitly. Missing target fields or an invalid selected config fail before the listener binds.

`cluster.namespace_mode` defaults to `shared`, which uses the configured namespace exactly. `worktree` resolves the configured base and the current worktree's stable identity to `<base>-wt-<identity>`; long bases are shortened with a hash. The dashboard uses the same resolver as `up` and `bootstrap`, so it reads the actual worktree namespace. An explicit `--namespace` is an exact target unless `--namespace-mode worktree` also requests derivation.

For another config, a chosen port or a headless session:

```sh
podgrove web --project-directory /path/to/worktree --config settings/dashboard.yml
podgrove web --context your-explicit-kube-context --namespace team-development --port 8765 --no-open
```

`--project-directory` defaults to the current directory, and `--config` defaults to its `podgrove.yml`. Dashboard target loading needs no Compose file and does not open referenced Compose/env/secret files or launch Docker.

`--port 0`, the default, selects an available port. The server binds only to `127.0.0.1`. There is no public listener, Kubernetes Service, separate frontend server, Node dependency, or frontend build step. HTML, CSS, and JavaScript ship with the Python package. The interface supports light and dark themes. It follows your system setting until you choose a theme in the toolbar, then remembers that choice in this browser.

## What it shows

The worktree list comes from this machine's validated Podgrove state for the selected context and resolved namespace. It does not discover environments started from another laptop or search arbitrary cluster namespaces. If you use `PODGROVE_STATE_HOME` for sessions, launch the dashboard with the same setting.

Sidebar entries show the worktree name, saved status and current branch when available; otherwise the secondary label uses the saved namespace. The selected worktree's name appears in the topbar. A compact row shows cluster, namespace, namespace mode, node placement, running services, storage and engine state. Branch and worktree names appear beside a disclosure for the full local path. Git metadata comes from the local checkout when it is available; unavailable metadata is labelled rather than guessed. Legacy records without a saved namespace mode are not assigned an inferred mode by the dashboard. Saved health is explicitly a **local snapshot**; its read timestamp is not a claim that the stack's health was just checked. The API keeps lifecycle timestamps separate from observation timestamps. The sidebar can collapse or expand and remembers that choice in this browser; narrow screens provide a menu for switching worktrees.

Selecting a worktree reads its owned StatefulSet, engine Pod, and PVC, then reads current Compose containers through that session's existing Docker connection. The detail view shows:

- Compose service state, health, replicas, image, and forwarded application ports.
- Engine Pod readiness, restart count, node, and observed CPU/memory requests and limits. Configuration → Resources also shows ephemeral storage and initializer allocations.
- PVC phase, requested and allocated capacity, and class/bound-volume names reported by the owned PVC. No StorageClass or PV object is read.
- Snapshot or live logs for one selected Compose service or the engine's Docker container, with a fullscreen reader.

The dedicated **Endpoints** tab lists application forwards and filters by service name, target port or local port. Addresses are selectable text with a copy control. Each row shows the supervisor’s forward state (`ready`, `reconnecting`, `disconnected` or `unknown`); the dashboard itself makes no application probes. A stale snapshot does not establish tunnel health. A saved list larger than the display bound reports that entries were omitted. No endpoint is presented as healthy merely because its saved address exists.

Allocation values are configured resources and PVC capacity, not live CPU, memory, or filesystem-usage metrics. When the session or cluster cannot be read, the page keeps the saved snapshot and reports the limitation; it does not silently convert stale information into fresh health.

## Logs

Choose the engine, a Compose service or an individual service container in the **Logs** tab. **Refresh logs** reads a snapshot; **Go live** opens a continuing connection using `kubectl logs --follow` or the existing authenticated Docker connection. The interface offers an initial tail of 100 or 200 lines per container, or **All logs** for all available retained history from the selected source. The API accepts 1–200 or the exact `all` value. This does not combine unrelated sources or recover rotated logs. Quiet streams remain connected and send heartbeats; the page does not repeatedly fetch snapshots to simulate live logs.

Live connections last up to five minutes. **Pause live** closes the connection; **Resume live** opens another connection with a fresh tail. The page reports disconnects, expiry, unavailable sources and changed ownership. Resuming can repeat lines or leave gaps; there is no durable cursor or exactly-once guarantee. A connection follows the selected container identities, so replacement containers require a new connection. Live service streams aggregate at most eight containers; larger services require a specific container selection rather than silently omitting replicas. Service snapshots read at most 16 containers within a shared 15-second budget and report truncation when a bound is reached. Aggregated output is not a globally timestamp-sorted log.

**All logs** still uses the dashboard limits: snapshots read at most 64 KiB and show an incomplete-output message when clipped. Docker snapshots can include the beginning of retained history before reaching that cap; they are not guaranteed to show its newest 64 KiB.

The reader keeps at most 2,000 lines and 256 KiB, discarding the oldest displayed lines with a notice. Auto-scroll can be disabled while reading; **Jump to latest** returns to the newest output. Wrapping and copying apply to the retained text. **Fullscreen** expands the reader, using browser fullscreen where supported and an in-page fallback otherwise. Escape exits. Changing the worktree, log source, tab or configuration page stops the active stream.

For retained service history without dashboard byte or line limits, export directly from the same worktree and context:

```sh
podgrove logs SERVICE --tail all > service.log
podgrove logs SERVICE --tail all --follow
```

These commands stream Compose output directly to stdout, preserve its exit status, and use the recorded environment even when the session is disconnected. CLI output does not apply dashboard redaction. Engine-container history can be exported with an explicitly targeted `kubectl logs --tail=-1` command. Neither path can recover logs already removed by the runtime's retention policy.

Only complete live-log lines are emitted. Lines larger than 16 KiB and incomplete final lines are discarded; the page reports these limits. Both snapshot and live reads redact common single-line password/token/authorization patterns, including Basic and Bearer credentials. Live reads also suppress private-key blocks across transport chunks and timestamp-prefixed lines, including when a delimiter occurs in an oversized discarded line. Snapshot redaction does not assemble multiline private-key blocks. None of these filters guarantees that application logs contain no secrets.

## Configuration page

The **Configuration** button at the bottom of the sidebar opens a separate read-only page and stays available when the sidebar is collapsed. Tabs separate **Cluster & namespace**, **Service accounts & access**, **Worktree**, and **Resources**. The page shows the explicit cluster context and a namespace dropdown restricted to the dashboard's resolved configured/flag namespace filter or this machine's validated environment records. With no namespace in that scope, it explains how to supply a namespace and performs no cluster reads.

For a selected namespace, the page shows its configured name and the observed `podgrove-bootstrap` provisioning ConfigMap, including version, mode and worktree identity when applicable. It never reads Namespace metadata. The response's `namespace` field remains `null`; `provisioning` holds the marker observation. Expandable entries show only the two known ServiceAccounts, two Roles and three RoleBindings in that namespace. These eight exact named GETs include the marker, with no node, StorageClass, PV or cluster-RBAC reads. Arbitrary bindings and referenced roles are never discovered or followed. Entries distinguish present, missing, inaccessible and not checked within the budget; an unreadable object is not reported as absent.

These are observed declarations, **not effective permissions** or proof that a namespace is a safe trust boundary. Other grants and admission policies can affect access. A valid marker records the intended shared/worktree mode; missing or incompatible markers block startup. `podgrove bootstrap --output <new-folder>` renders nine namespaced setup objects for review and installation into the already prepared target. It needs no StorageClass selection. The page never generates or installs resources. Normal cleanup retains the namespace and bootstrap, and no Podgrove operation reads or mutates a Namespace object.

The worktree dropdown on this page shows a safe projection of that worktree's **current YAML file**. It includes format version, configured context, namespace base, namespace mode and StorageClass, size, node placement mode, idle TTL, Compose filenames/profiles/project directory, sync exclusions, and forwarding configuration. Configured network exclusions are shown alongside the saved launch exclusions. The Resources tab compares current engine and initializer requests/limits and PVC capacity with observed allocations, including ephemeral storage. The observed context and actual namespace remain separate from those configured values. Tainted placement also shows its validated selector and taint key/value/effect. Reading or editing that file does not apply its settings to an existing environment. Missing or invalid files are identified without replacing the saved runtime values or displaying raw file contents.

Configuration inspection is deliberately bounded:

- The selected config must be an owned regular file beneath an owned worktree, with no symlink traversal or hard links. Reads use a 64 KiB limit and reject files that change while being read.
- YAML aliases, custom tags, duplicate keys, excessive nesting/node counts and invalid schema values are rejected. Referenced Compose files and environment/secret files are not opened; shell interpolation is not executed.
- Cluster configuration uses named GETs only, at most four at once, with a shared 15-second read budget and per-read bounds. Rules and subjects can be truncated with an explicit notice. No token references, arbitrary annotations, kubeconfig credentials or raw CLI errors are displayed.

## Read-only behavior and access

Dashboard API routes accept GET only. They do not call the lifecycle `status` command, touch activity, write local state, create tunnels, restart services, execute application commands, or create/delete cluster resources. Refreshing the page and reading logs do not extend idle TTL. An environment can expire while its dashboard is open.

Engine reads use the explicit context and names recorded for that worktree. Before exposing live engine information or logs, the backend checks Podgrove labels, the Pod's real StatefulSet owner reference, and its PVC relationship. Service logs are selected from existing Compose-labeled containers; the browser cannot supply a command or arbitrary file path. The Configuration page separately reads only its exact known namespace/access objects within the scope described above. The dashboard needs the existing user's metadata/log read permissions and an authenticated running session for Docker reads. It does not install RBAC or platform templates.

Each server process generates a random API token in the URL fragment. The frontend removes that fragment after loading and keeps the token in session storage for the same origin so refresh works. API requests send it in a dedicated header. Keep the printed URL private; restart the dashboard to replace the token. The backend also checks the exact Host and same-origin browser requests, disables framing, and sends no CORS permission or cached API responses.

Responses omit session tokens, control sockets, Docker connection addresses, container command/env payloads, and private authentication configuration. Application logs are intentionally displayed: common password/token/authorization patterns are redacted, but **arbitrary secrets in application logs cannot be reliably identified**. Review application logging practices before sharing a screenshot or log excerpt.

The snapshot log-tail setting accepts 1–200 lines per selected container or `all` retained history; combined output is capped at 64 KiB per request, with truncation reported. JSON responses are capped at 1 MiB. Live logs use an authenticated NDJSON endpoint, with two stream slots separate from four ordinary request slots, a bounded server queue and a five-minute connection lifetime. Ownership is revalidated before exposing logs and periodically during a stream. Disconnects and shutdown cancel upstream subprocesses/sockets. Cluster subprocesses, Docker reads, and concurrent HTTP requests have explicit limits; no mutation controls are exposed.

## Local verification

Backend tests use owned local fixtures and fake cluster responses. Browser tests launch a fresh browser profile against an inert HTTP provider; they do not contact Kubernetes or Docker:

```sh
uv sync --extra test --extra web-test
.venv/bin/pytest tests/test_web.py tests/test_web_settings.py tests/test_web_streaming.py tests/test_web_browser.py -q
```

The browser tests use installed Chrome when available, otherwise Playwright Chromium. `PODGROVE_BROWSER_EXECUTABLE` selects another compatible browser executable. If no browser/dependency is installed, those optional tests skip rather than claim a browser pass. Screenshots are written under `artifacts/ui/`.

Namespace selection, safe storage/mode projection, exact namespaced reads, provisioning-marker checks and browser display have local regression coverage. Run authorized live checks separately for your target; local fixture results do not prove cluster admission or service reachability. See [verification](verification.md).
