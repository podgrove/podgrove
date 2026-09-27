# Development workflows

The CLI authenticates using the credentials of the explicitly selected kubeconfig context. That context can use a personal identity or a namespace-only ServiceAccount; Podgrove never substitutes another identity to bypass a denial. See the [administrator guide](../deploy/README.md) for the five-file namespace-only setup.

## Run the same stack on an approved target

Keep the original Compose files and their ordering. Set the actual approved context, existing namespace and dynamic StorageClass in `podgrove.yml`; there is no built-in target. The administrator must verify storage reclaim behavior because the runtime never reads StorageClass or PersistentVolume objects. Generate/review/apply the bootstrap folder once per shared namespace or once per derived worktree namespace as described in [getting started](getting-started.md#configure-the-target-and-prepare-access).

From the configured application workspace:

```sh
podgrove validate
podgrove doctor
podgrove up
podgrove status --json > /tmp/podgrove-status.json
podgrove env --json > /tmp/podgrove-endpoints.json
podgrove logs api --tail 100
```

Replace `api` with a service in your Compose model. Run the application's unchanged tests using the exported host/port values. For example, if Compose publishes `api:80`, shell consumers can use:

```sh
eval "$(podgrove env)"
curl --fail "$PODGROVE_API_80_URL/health"
```

The route must exist in that application. MongoDB and Redis need their own URI scheme and the project's test credentials; an HTTP convenience URL is not a database connection string. Use `status --json` for original service names or if normalized export names collide. Endpoint exports refuse unhealthy local forwarding; they do not claim the application is healthy. Capture the original test report, verify sync with a reversible source edit, call `up` again to check idempotence, and finally `down` the same worktree. `down` removes that engine's PVC data and retains the namespace/bootstrap.


## Backend and web in one lane

Select a common workspace root that contains both real repository worktrees. Each lane gets its own common root and therefore its own engine/PVC identity:

```text
lane-a/
  podgrove.yml
  lane.env
  backend/               # real backend worktree
  web/                   # real web worktree
```

A configuration in `lane-a/podgrove.yml` can point at both original files:

```yaml
version: 1
cluster:
  context: your-approved-context
  namespace: your-existing-namespace
  storage_class: your-approved-dynamic-class
compose:
  project_directory: backend
  files: [backend/compose.yml, web/compose.overlay.yml]
  env_file: lane.env
size: medium
ttl: 12h
sync:
  exclude: [__pycache__, "*.pyc", .pytest_cache]
```

Run `podgrove up --project-directory /absolute/path/to/lane-a`, or run from `lane-a`. This common lane directory must be outside an enclosing Git checkout if it is intended to define its own identity. Inside Git, identity always follows the nearest checkout top level, including when `--project-directory` names a subdirectory. The selected configuration directory bounds allowed source paths, and YAML `compose.project_directory` selects Compose's path-resolution base within it. Compose still resolves relative paths in both overlays against that base. Supply any existing overlay variables such as `WEB_CONFIG_DIR` in `lane.env` with their stable absolute path inside this lane. Files outside the configuration boundary, or symlinked repository directories, remain unsupported. No synthetic checkout/copy or external-root exception is needed when both worktrees live under the lane root. The [real Compose regression](../tests/test_lane_workspace.py) verifies the two-file model and bind sources.

Keep interpolation inputs identical across `validate`, `up`, status/logs, and later refreshes. An env-file is convenient; shell variables override Compose's env-file interpolation, so remove conflicting exports. Changing a PID-derived project name, cookie name or resource variable changes the normalized model and legitimately triggers refresh. Unchanged cold/first/second `up` and the older pre-network fingerprint format are covered by [fingerprint regressions](../tests/test_fingerprint.py). The compatibility match is limited to default network settings and no sync exclusions; meaningful configuration changes still refresh. The historical one-time change is consistent with an editable-source upgrade, but its precise cause remains unproven.

For one application repository, commit `podgrove.yml` once at its root instead of generating a file per checkout. Relative Compose paths resolve within each linked worktree, while runtime identities and endpoints remain separate. See the [portable example](../examples/portable/README.md); its test creates real Git worktrees and verifies that both retain byte-identical committed configuration. The surrounding multi-repository lane above remains an explicitly configured directory rather than an automatic sibling-worktree discovery feature.

## Resources, shared seats and TTL

Choose a preset or explicit `resources` and `storage.size` in each worktree's YAML. The engine budget covers builds, Docker and all nested services. Measure your own application and leave daemon/build headroom. Existing incompatible engine/PVC settings are refused before interrupting an active session; use a separately planned environment and data migration for a different allocation, not routine `down`. See [resource configuration](configuration.md#resource-sizes).

Each worktree gets a distinct engine even in one namespace. Separate OS users have separate local state; seats sharing an OS user share its default state directory. Bare `reap` now targets the current workspace; namespace-wide cleanup requires `--all`. Use explicit `--environment` for another exact identity. State-directory separation does not narrow a namespace-wide reaper.

Connected sessions enforce TTL too. Source/watch activity and successful connected status/logs/exec calls count as activity; application requests, browser dashboard reads and endpoint exports do not. Choose a TTL longer than an unattended test run. ServiceAccount credential expiry can disconnect a session independently of TTL; use the platform's supported credential refresh flow.

Tests that make many serial requests from the laptop incur Kubernetes-tunnel latency. Running tests inside an existing Compose service with `podgrove exec` can reduce round trips when its image contains the required dependencies. Measure with your own workload; there is no universal performance multiplier.

## Reconnect and retained diagnostics

A bounded supervisor retries dropped local forwarding on the original ports while checking engine ownership. After retry exhaustion or credential expiry, inspect `status` and reconnect with `up`; a changed or foreign engine is never silently adopted. Endpoint exports report forwarding readiness separately from service health.

Service log snapshots can be retrieved through a temporary ownership-checked Docker connection after a local session disconnects. Saved project/service metadata allows diagnosis even when local Compose sources have become invalid. Viewing the browser does not extend the idle TTL.
