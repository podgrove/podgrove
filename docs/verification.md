# Verification

Validate the revision you intend to use. Local unit tests, browser fixtures, disposable Docker tests and live Kubernetes acceptance prove different behavior. A local pass does not establish an arbitrary cluster's admission, storage or packet paths.

## Local checks

From a development checkout:

```sh
uv sync --locked --extra test --extra web-test
.venv/bin/playwright install chromium
.venv/bin/ruff check podgrove scripts tests
.venv/bin/pytest -q -m 'not integration and not cluster' --junitxml=artifacts/local-tests.xml
.venv/bin/python scripts/verify_docs.py --output artifacts/docs-new.json
```

Use a fresh output filename for documentation evidence. On Linux, browser setup may need `playwright install --with-deps chromium`. Keep the Docker Compose CLI available for semantic configuration checks, and GNU coreutils for macOS receiver-shell tests. Missing dependencies can cause skips; record the actual test summary.

Coverage includes explicit targets and bootstrap permissions, two-worktree identities, custom resource/PVC settings, safe reconnect/refusal, ownership checks, source churn, real local process/listener recovery faults, configuration parsing and browser interactions against an inert provider. Local tests do not deploy to Kubernetes.

## Disposable Docker tests

Follow [the test-lane guide](../tests/README.md) to opt into the local Docker engine matrix and transport checks. The harness labels its disposable engines and removes only its own resources. It requires a compatible local Docker daemon and sufficient disk space. It never invokes Kubernetes.

## Package and publication checks

The [release runbook](releasing.md) defines version parity, archive inspection, isolated wheel installation and Homebrew formula checks. Test the installed wheel outside the source checkout so a missing packaged module or static asset cannot be hidden by editable imports. Publish checksums for the exact uploaded bytes.

Internal deployment evidence, workstation configuration and private issue exports are not part of the public source distribution. The explicit publication manifest and export checks prevent those local files from entering the public snapshot. The known-pattern scan is an additional guard, not proof that every possible secret format has been detected.

## Live acceptance

A namespace administrator must review the [platform prerequisites](../deploy/README.md), prepare the exact namespace, and authorize live tests independently. Use the intended namespace-scoped identity to check `doctor`, startup, application health, forwarding recovery, persistence and cleanup. Test positive and negative traffic paths against the actual cluster's NetworkPolicy implementation.

Record the source/package version, explicit target, observed resource identities, outcomes and cleanup. Keep credentials out of reports. Namespace-only PVC deletion cannot prove backing-volume deletion. Never run a broad reaper or alter another worktree as part of validation.
