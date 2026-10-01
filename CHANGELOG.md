# Changelog

User-visible changes are recorded here. Release Please manages versioned entries after the first public release.

## [0.4.1](https://github.com/podgrove/podgrove/compare/v0.4.0...v0.4.1) (2026-10-01)


### Bug Fixes

* **release:** resolve draft releases and bump uv.lock; v0.4.0 was tagged but never published ([#9](https://github.com/podgrove/podgrove/issues/9)) ([e32ba9e](https://github.com/podgrove/podgrove/commit/e32ba9ef55b2fb892a76f9bd89b4fb541df0535d))

## [0.4.0](https://github.com/podgrove/podgrove/compare/v0.3.0...v0.4.0) (2026-10-01)


### Features

* configurable pod-to-pod networking (disabled/open/selected) ([1175ccc](https://github.com/podgrove/podgrove/commit/1175cccf43d2140a03bdc5af3fb0ee5eabdbe118))


### Documentation

* clarify the source sync boundary ([e317cf0](https://github.com/podgrove/podgrove/commit/e317cf0194668ffec40107ee8ff04d8aed4d6c75))
* publish the Podgrove website ([3105e35](https://github.com/podgrove/podgrove/commit/3105e35ec00622e9f4c67ad715a69ef5242e71c2))
* simplify the architecture overview ([e2890b0](https://github.com/podgrove/podgrove/commit/e2890b077c47f2fa3e69e8a9ef7b5f581437e39c))

## 0.3.0 (2026-09-28)

- Protect each engine with supported autoscaler annotations and a namespaced PodDisruptionBudget; add optional node selectors, node affinity and tolerations for existing pools.
- Retry startup after a verified engine Pod replacement within a fixed budget, preserving the original StatefulSet specification and PVC identity.
- Remirror bind sources on rerun and recreate failed services while keeping healthy services available; retain diagnostic sessions and forwards for running containers after partial startup.
- Add declared reverse TCP forwarding to laptop loopback services and exact same-namespace environment/service links with narrow NetworkPolicy rules.
- Suggest `sync.exclude` when a mirrored source contains an unsupported symlink, and include new configuration fields in the read-only dashboard.

## 0.2.7 (2026-09-28)

- Return one JSON document for `doctor --json` success, check/configuration errors and interruption, while preserving ordinary text output.
- Flush fixed startup phase messages to the session log before engine readiness, source copy, Compose build/readiness and forwarding work, so callers can inspect a slow startup.

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
