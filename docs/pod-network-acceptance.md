# Pod-to-Pod acceptance plan

This is a test plan, not evidence that a cluster passed. Run the reviewed acceptance harness only after an operator authorizes the existing namespaces and supplies their current bootstrap, storage and namespaced discovery access. The full matrix needs two distinct namespaces; `--same-namespace-only` limits the run to one and explicitly leaves cross-namespace coverage unproved. No part of this plan creates namespaces or needs Namespace, node, PV or cluster-RBAC reads.

## Run the reviewed harness

Select an absolute executable installed from a verified wheel and use its matching source checkout:

```sh
python3 scripts/check_pod_network.py --execute \
  --podgrove-bin "$PODGROVE_BIN" \
  --context your-development-context \
  --namespace-a your-first-development-namespace \
  --namespace-b your-second-development-namespace \
  --storage-class your-approved-storage-class \
  --output /path/to/new-private-pod-network-evidence
```

For an authorized single namespace, omit `--namespace-b` and add `--same-namespace-only`. This creates no second-namespace fixtures and makes no second-namespace reads; wrong-namespace and cross-namespace checks are reported as untested. The default run requires distinct prepared A/B namespaces. `--cluster-domain` can record the operator's actual DNS suffix, and `--settle-timeout` bounds policy convergence (default 60 seconds). It does not change the product's reported `cluster.local` endpoint contract.

The supplied `KUBECONFIG` is inherited. If an external command reports an authentication failure, the harness stops all later external commands, including cleanup; it does not retry with another credential or context. Evidence records the unconfirmed owned resources and required owner action. Restore authorized access and review that scope before any later cleanup; an authentication failure is not a passed cleanup.

## Disposable fixtures

Run at most two disposable engines at once; the single-namespace option stops after its same-namespace phase. Keep one fresh client worktree in namespace A. First start a fresh server in A and run the same-namespace matrix; then use scoped `down` and prove that server's exact resources and process are absent before starting a new server in namespace B. Use separate private state directories for all three worktrees and never reuse a developer's running stack. Both namespaces belong to the explicitly selected cluster. Keep generated configs, command logs, observed UIDs and results in a new private evidence directory; never save credentials in public fixtures.

Each engine uses a small explicit resource budget and its own PVC. The server's Compose fixture has an exposed HTTP service on container port 8080 and a separate unexposed service on 8081, published as `0.0.0.0:18080:8080` and `0.0.0.0:18081:8081`, plus a client with bounded TCP/HTTP probes. Distinct response markers identify the server that answered. Worktree names deliberately match or miss the tested patterns.

Before denying traffic, prove both published listeners answer from another open engine over Pod IP and the reported headless-Service DNS. This prevents a stopped server or wrong port from looking like successful isolation. Confirm DNS resolves the intended Pod IP. Compare the cluster resolver suffix (or an explicit operator value) with the reported endpoint. Product endpoint names currently assume `cluster.local`; a custom suffix must be reported as an unsupported endpoint limitation, not counted as a successful reported-DNS check.

## Required matrix

| Scenario | Expected result for fresh TCP connections |
| --- | --- |
| Both peers open | Published listeners work by Pod IP and DNS in the same namespace, and across namespaces only when each side lists the other's namespace in `network.namespaces`. |
| Open source without the target namespace listed | Denied in the cross-namespace phase; open never matches every namespace. |
| Disabled source, open target | Source egress is denied; localhost forwarding and same-engine Compose traffic still work. |
| Open source, disabled target | Target ingress is denied. |
| Selected source and target, matching namespace/worktree/port | Declared port works by reported DNS and Pod IP. |
| Correct destination but undeclared published port | Denied against the independently verified listening port. |
| Wrong namespace or nonmatching source/target worktree pattern | Denied against the same known listening server; no extra engine is needed. |
| Only source declares the link | Denied. |
| Only target declares the source | Denied. |
| One endpoint open and the other selected | Denied; selected links require matching declarations in selected mode at both ends. |
| Omitted target `from.worktree` or explicit wildcard | All managed peers in that named namespace can match, while source-side permission remains necessary. |
| Open → selected → disabled | Old broad access closes for new connections within a bounded observation window. |
| Rule removed or peer discovery becomes unavailable | Status reports the result and no stale/broader selected grant is accepted as success. |
| One invalid, foreign or terminating peer | That peer is skipped and listed as pending; every other link keeps its grant. |

Negative probes must time out against a listener proven reachable in the positive baseline; a refused connection is not proof of policy enforcement. Do not retry a mutating application request. Poll bounded read-only probes to allow asynchronous CNI policy propagation and retain every intermediate outcome. Existing TCP connections are not evidence of revocation because their treatment is implementation-defined.

## Offline refusal cases

Without Kubernetes or a Docker daemon, run `validate` and `up --dry-run --json` against missing services, disabled-profile services, Compose `expose` without publication, loopback-only binds, UDP, target-only/zero/ranged publication, duplicated target publication, scaled exposed services and reserved Docker API ports. Check unknown keys at every nesting level, invalid modes, expose/connect outside selected mode, namespace globs, malformed worktree patterns, duplicate/out-of-range ports and legacy top-level links. Ordinary dynamic localhost forwarding must continue to validate when it is not selected exposure.

Controlled runtime regressions separately cover a self-labelled Pod in an unlisted namespace, an oversized declaration, a replaced own Pod, stale/malformed/foreign peer metadata, changed UIDs, unauthorized namespace reads, failed inventory and legacy source-owned grants. Do not manufacture these faults against an unrelated live workload.

## Completion and cleanup

Record the exact installed executable and package/source identity, both explicit namespaces, all three sequential worktree names/identities, initial object UIDs, positive/negative probe results, observed DNS, policy revisions and status. A namespace-only pass proves those tested packet paths on that cluster; it does not prove cloud-volume deletion or all CNI configurations.

The same-namespace server must be cleaned before the cross-namespace phase begins. In `finally`, preserve evidence and, only while authentication remains usable, run scoped `down` for each captured fixture identity still present. An authentication-failure latch forbids further cluster commands and leaves cleanup explicitly unconfirmed. Refuse changed or unverified ownership. Verify exact-name and managed-label resource absence, local state removal and captured supervisor termination. Keep both namespaces and bootstrap resources. Cleanup failure remains a failed result with diagnostics; it is never permission for broad deletion or `reap --all`.
