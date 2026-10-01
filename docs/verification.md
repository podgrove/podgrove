# Verification

Validate the revision you intend to use. Local unit tests, browser fixtures, disposable Docker tests and live Kubernetes acceptance prove different behavior. A local pass does not establish an arbitrary cluster's admission, storage or packet paths.

## Local checks

Run repository checks from the **Podgrove checkout**. Install both test extras and a browser for the UI checks:

```sh
uv sync --locked --extra test --extra web-test
.venv/bin/python -m playwright install chromium
.venv/bin/pytest -q -ra -m 'not integration and not cluster' --junitxml=artifacts/local-tests.xml
.venv/bin/ruff check podgrove tests scripts
.venv/bin/python scripts/verify_docs.py --output artifacts/docs-new.json
```

Keep the Docker Compose CLI available for semantic configuration checks and GNU coreutils for macOS receiver-shell tests. On Linux, browser setup may need `playwright install --with-deps chromium`.

Pip users can install `-e '.[test,web-test]'` with the same virtual environment instead. The browser tests use a fresh disposable profile and inert local fixtures; they do not use Kubernetes or Docker. They can use installed macOS Chrome, Playwright Chromium, or `PODGROVE_BROWSER_EXECUTABLE`. Missing Playwright/browser dependencies cause skips, so inspect the test summary rather than counting a skipped browser suite as passed.

Use a fresh output filename for documentation evidence. Coverage includes explicit targets and bootstrap permissions, two-worktree identities, custom resource/PVC settings, safe reconnect/refusal, ownership checks, source churn, real local process/listener recovery faults, configuration parsing and browser interactions against an inert provider. Local tests do not deploy to Kubernetes.

## Disposable Docker tests

Opt-in local Docker checks require a local Unix-socket engine with privileged-container support:

```sh
PODGROVE_DOCKER_TESTS=1 .venv/bin/pytest tests/test_sync.py -q
export PODGROVE_E2E_OUTPUT="$(mktemp -d /tmp/podgrove-docker-e2e.XXXXXX)"
.venv/bin/python scripts/e2e.py --mongo --output "$PODGROVE_E2E_OUTPUT"
```

The sync tests use disposable Docker volumes. The end-to-end harness creates bounded, uniquely named Docker-in-Docker engines, compares two isolated worktrees, and cleans up its resources. It never invokes Kubernetes. `--mongo` adds MongoDB isolation to Redis and application checks; results and command logs go to the fresh output directory. Keep any evidence you need; do not reuse a directory containing an earlier run's results.

To include both Docker lanes (including Mongo) in the complete local pytest run:

```sh
PODGROVE_DOCKER_TESTS=1 PODGROVE_RUN_DOCKER_E2E=1 .venv/bin/pytest -q -ra -m 'not cluster'
```

Follow [the test-lane guide](../tests/README.md) to opt into the local Docker engine matrix and transport checks. The harness labels its disposable engines and removes only its own resources. It requires a compatible local Docker daemon and sufficient disk space. It never invokes Kubernetes.

The missing-file startup fixture has a separate opt-in check:

```sh
PODGROVE_RUN_DOCKER_E2E=1 .venv/bin/pytest tests/integration/test_startup_recovery_docker.py -q
```

It uses one disposable local Docker-in-Docker engine: one service exits, another remains unhealthy, and a healthy service stays running. After creating the required local file, a new startup must mirror it before recovering the failed services while preserving the healthy container. This does not test Kubernetes Pod replacement, admission or eviction protection.

## Package and publication checks

`scripts/verify_package.py --output /tmp/podgrove-package-new` builds and verifies installed wheel/source archives offline using locally available build/dependency files. Optional `--deliver` preserves the previous matching archives before placing the verified pair in `dist/`; run it after source and documentation stop changing.

The [release runbook](releasing.md) defines version parity, archive inspection, isolated wheel installation and Homebrew formula checks. Test the installed wheel outside the source checkout so a missing packaged module or static asset cannot be hidden by editable imports. Publish checksums for the exact uploaded bytes.

Internal deployment evidence, workstation configuration and private issue exports are not part of the public source distribution. The explicit publication manifest and export checks prevent those local files from entering the public snapshot. The known-pattern scan is an additional guard, not proof that every possible secret format has been detected.

## Live acceptance

A namespace administrator must review the [platform prerequisites](../deploy/README.md), prepare the exact namespace, and authorize live tests independently. Use the intended namespace-scoped identity to check `doctor`, startup, application health, forwarding recovery, persistence and cleanup. Test positive and negative traffic paths against the actual cluster's NetworkPolicy implementation.

Local checks do not authorize cluster changes. Podgrove never supplies a default test cluster. Verify application health, allowed DNS/egress, denied peer traffic, PVC persistence and exact owned cleanup on the approved target.

Record the source/package version, explicit target, observed resource identities, outcomes and cleanup. Keep credentials out of reports. Namespace-only PVC deletion cannot prove backing-volume deletion. Never run a broad reaper or alter another worktree as part of validation.

### Connectivity and startup recovery

Run the following scripts from a matching source checkout, with `PODGROVE_BIN` set to an **absolute, versioned executable installed from a verified wheel**. Select the approved namespace-scoped kubeconfig through `KUBECONFIG`; the scripts inherit it. The namespace must already have the current reviewed bootstrap/RBAC bundle, including PodDisruptionBudget access and Pod/StatefulSet annotation patch permissions. Its admission and storage must support the privileged engine and the explicit storage class. The startup and Pod-to-Pod scripts do not prepare namespaces or read Nodes.

Each invocation needs a new, nonexistent output directory beneath an existing parent. `--execute` is required for live changes:

```sh
python3 scripts/check_startup_recovery.py \
  --podgrove-bin "$PODGROVE_BIN" \
  --context your-development-context --namespace your-development-namespace \
  --storage-class your-approved-storage-class \
  --output /path/to/new-private-startup-evidence --execute
```

The retained `check_connectivity.py` script covers the retired top-level `connect` model and is not a current live acceptance command: the new binary refuses its legacy declarations. Its historical results remain attributed to the tested version. Reverse transport regressions still cover the supported laptop-loopback feature. For `network.pod_to_pod`, follow the [current acceptance plan](pod-network-acceptance.md): at most two engines run simultaneously, and `--same-namespace-only` explicitly omits cross-namespace coverage.

The startup check creates **three fresh engines serially**, cleaning up each before starting the next. It checks ordinary `up` and `up --refresh` after a required file appears, retained diagnostic access, and preservation of the healthy container. On its first engine it removes each protection annotation from the Pod and StatefulSet template independently, then deletes the exact PDB; `doctor` must identify each missing safeguard and `up` must repair it. On the third it observes a unique build process, deletes only that owned Pod with UID/resource-version preconditions, and requires a successful startup retry with unchanged controller/PVC UIDs. This proves detection and repair, not autoscaler behavior or survival of physical node failure.

The startup and Pod-to-Pod scripts use private temporary worktrees/state, record commands and results, and attempt explicit scoped `down` in cleanup. The Pod-to-Pod harness stops all external commands after an authentication failure, including cleanup, and reports the remaining scope for owner review; it never switches credentials or contexts. Success requires exact-name and labelled-resource absence, local state removal and captured-supervisor absence; the namespace and bootstrap stay in place. Cleanup refusal or failure is a failed result with retained diagnostics, not permission to delete more broadly. Review `result.json` and the command logs. Binary path/version recording does not replace independent wheel checksum and installation verification.
