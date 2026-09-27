# Changelog

User-visible changes are recorded here. Release Please manages versioned entries after the first public release.

## 0.2.1 (2026-09-27)

- Stream binary exec exports directly with WebSockets, preserve exit status, and isolate failed Docker API connections without replaying commands.
- Retain sessions through transient status/ownership/heartbeat read failures, enforce bounded ownership-proof age, and cancel stalled verification subprocesses safely.
- Record captured and observed engine UIDs, reporting Pod replacement during failed builds even when restart counts reset.
- Resolve target-only and dynamically published Compose ports, preserve unchanged legacy fingerprints, and emit JSON cleanup results.
- Derive identity from the Git worktree root and support one committed configuration across linked worktrees.
- Add All logs and unrestricted CLI history exports, with honest dashboard display bounds, and unify focus borders in both themes.
- Add release-bound exact-byte exec and four-hour forwarding acceptance runners. Test results are separate evidence; merely shipping a runner does not establish a completed soak.
- Resolve authenticated draft releases without replacing published assets.

## 0.2.0 (2026-09-27)

- Publish the first tagged release with verified source and wheel archives, checksums and source provenance.
- Install releases into separate versioned environments and atomically select a stable executable after validation; preserve running sessions and previous installations.

- Run existing Compose worktrees on separate Kubernetes Docker engines and PVCs.
- Configure shared or per-worktree namespaces, optional tainted-node placement, resource budgets and storage capacity.
- Generate namespace-scoped bootstrap manifests and supervise file synchronization and local TCP forwarding.
- Inspect services, endpoints, storage, configuration and snapshot/live logs in a local read-only dashboard.
- Read live logs in fullscreen with pause/resume, bounded history, wrapping and copy controls.
- Show branch/worktree identity in a compact dashboard with light/dark themes, a collapsible sidebar, configuration tabs and the Pods logo.

The Homebrew tap is not published yet. Use the verified release installer described in the README.
