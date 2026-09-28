# Changelog

User-visible changes are recorded here. Release Please manages versioned entries after the first public release.

## 0.2.6 (2026-09-27)

- Clarify bounded idle file-sync recovery versus paused uncertain transfers, including when `up --refresh` is required.
- Correct shared-node eligibility and distinguish resource presets from custom budgets.
- Documentation and release metadata only; runtime behavior, dependencies and the acceptance runner are unchanged.

## 0.2.5 (2026-09-27)

- Recover idle file-sync transport loss with bounded, cancellable attempts against the original engine and verified sync helper, retaining healthy session commands and application forwards.
- Verify the last acknowledged remote baseline before resuming. Pause uncertain transfers or exhausted recovery without replaying a batch or restarting application services.
- Show sync recovery and paused diagnostics in CLI status and the dashboard's Engine tab; keep process interruption, ownership changes, cancellation and uncertain outcomes covered by regression tests.
- Record bounded sync-only recovery explicitly in the forwarding acceptance runner, with independent control, HTTP, process and ownership checks. Shipping the runner does not establish a completed soak.

## 0.2.4 (2026-09-27)

- Accept complete dashboard Docker HTTP responses, including `Connection: close` responses whose declared body has been fully consumed, without reading a closed socket again.
- Reject premature EOF when a response still owes declared bytes, while preserving explicit dashboard size-limit clipping. This response-completion fix does not establish the cause or resolution of historical Docker-tunnel log resets.

## 0.2.3 (2026-09-27)

- Shorten the README around the problem Podgrove solves, its operating model, and the first successful deployment.
- Keep detailed setup, Compose migration, operation, recovery and verification guidance in linked documentation.
- Verify complete non-TTY exec output with separate stdout/stderr checksums and a drain acknowledgement, preventing a successful transport exit from hiding truncated exports. Interrupted commands are never replayed.
- Preserve independent output backpressure and cancellation without changing the caller's file-descriptor flags; keep native interactive terminal behavior.

## 0.2.2 (2026-09-27)

- Start supervisors with Python import isolation so an older checkout in the caller's working directory cannot replace the installed runtime.
- Verify this behavior with real subprocesses and require the isolated supervisor command in the forwarding soak.

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
