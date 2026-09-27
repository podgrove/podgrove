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

## Package and publication checks

`scripts/verify_package.py --output /tmp/podgrove-package-new` builds and verifies installed wheel/source archives offline using locally available build/dependency files. Optional `--deliver` preserves the previous matching archives before placing the verified pair in `dist/`; run it after source and documentation stop changing.

The [release runbook](releasing.md) defines version parity, archive inspection, isolated wheel installation and Homebrew formula checks. Test the installed wheel outside the source checkout so a missing packaged module or static asset cannot be hidden by editable imports. Publish checksums for the exact uploaded bytes.

Internal deployment evidence, workstation configuration and private issue exports are not part of the public source distribution. The explicit publication manifest and export checks prevent those local files from entering the public snapshot. The known-pattern scan is an additional guard, not proof that every possible secret format has been detected.

## Live acceptance

A namespace administrator must review the [platform prerequisites](../deploy/README.md), prepare the exact namespace, and authorize live tests independently. Use the intended namespace-scoped identity to check `doctor`, startup, application health, forwarding recovery, persistence and cleanup. Test positive and negative traffic paths against the actual cluster's NetworkPolicy implementation.

Local checks do not authorize cluster changes. Podgrove never supplies a default test cluster. Verify application health, allowed DNS/egress, denied peer traffic, PVC persistence and exact owned cleanup on the approved target.

Record the source/package version, explicit target, observed resource identities, outcomes and cleanup. Keep credentials out of reports. Namespace-only PVC deletion cannot prove backing-volume deletion. Never run a broad reaper or alter another worktree as part of validation.
