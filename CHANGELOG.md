# Changelog

User-visible changes are recorded here. Release Please manages versioned entries after the first public release.

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
