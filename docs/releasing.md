# Releasing Podgrove

Podgrove uses reviewed release pull requests, GitHub release assets, and an optional
Homebrew tap. Release automation is **off until a maintainer enables it**. Preparing
or pushing the repository does not publish a version, create a tap, install
Podgrove for existing users, or change their running environments.

The intended Homebrew command is `brew install podgrove/tap/podgrove`. It becomes
available only after the public `podgrove/homebrew-tap` repository exists and its
first verified `Formula/podgrove.rb` pull request has been merged. There is no
PyPI publication job in this repository.

## Maintainer setup

1. Push the reviewed public source to `podgrove/podgrove`, with private rollout
   notes, credentials, generated manifests, and local state excluded. Follow the
   [public source preparation guide](../publication/README.md) when exporting from
   an existing private development checkout. Passing package checks does not
   replace reviewing the public files and Git history.
2. Enable GitHub Actions and allow it to create pull requests. Protect `main`,
   require CI, and require review of release pull requests. Action dependencies
   use full commit SHAs; review Dependabot updates rather than replacing those
   pins with moving tags.
3. Create the `release` environment, restrict deployment to `main`, and configure
   required reviewers if publication must require a second maintainer. Enable
   [GitHub release immutability](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/prevent-release-changes)
   before the first release. The workflow uploads all assets to a draft before
   publishing; after publication it never replaces assets or moves a tag.
4. Optionally configure `RELEASE_PLEASE_TOKEN` as a fine-grained token limited to
   this source repository, with contents, issues, and pull-request write access.
   Without this secret, release-please uses `GITHUB_TOKEN`. That is sufficient for
   the publication jobs chained in this workflow. PR `opened`, `synchronize`, and
   `reopened` events created with that token produce approval-required CI runs:
   a maintainer with write access can select **Approve workflows to run** in the
   PR, or explicitly dispatch **CI** on its branch. Verify that the successful
   checks cover the PR's current head before merging; do not disable required
   checks. A scoped App token or PAT allows these PR runs to start without that
   approval prompt. Other events, including release and tag-push events, remain
   suppressed when created with `GITHUB_TOKEN`.
   [GitHub documents the event exceptions and approval behavior.](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow)
5. When the repository and workflow are ready, set the repository Actions variable
   `RELEASE_AUTOMATION_ENABLED` to the literal string `true`. Leaving it absent or
   setting it to `false` keeps both automatic and manual publication disabled.

The workflow grants read access by default. Only release-please can write release
PRs/tags; only the publication job can upload assets. Untrusted pull requests run
with read access and have no release or tap credentials. CI runs local tests and
an inert browser fixture; it never invokes live cluster tests or the destructive
Docker acceptance harness. Hosted macOS jobs install Docker CLI, the Compose
plugin, and GNU coreutils; Linux jobs use the hosted runner's CLI and GNU tools.
A required preflight fails if Compose or the receiver tools are missing, so those
semantic checks cannot silently skip. Jobs use an empty temporary Docker config
and an unreachable daemon address. Every matrix lane also validates the public
documentation, schema examples, offline bootstrap, and dry-run rendering through
`scripts/verify_docs.py`, preserving its report as a workflow artifact. These
runner setup steps do not install anything on a maintainer's machine.

## Version and release flow

Use Conventional Commits (`fix:`, `feat:`, and `feat!:` for breaking changes).
Release-please prepares a PR updating `CHANGELOG.md`, the Python package version,
`podgrove/__init__.py`, `.release-please-manifest.json`, and just the local
`podgrove` package version in `uv.lock`. Dependency versions remain locked.
The recorded `0.1.0` baseline is not evidence that a public GitHub release exists.
Review the proposed first public version and release notes explicitly.

Merging a release PR starts one workflow:

1. Release-please creates a stable `vX.Y.Z` tag and **draft** release.
2. The resolver verifies the tag and obtains an exact commit SHA. The reusable CI
   workflow tests that commit on Linux/Python 3.11 and 3.14, macOS/Python 3.14, and
   Chromium with an inert fixture provider.
3. The release build creates an sdist, then builds its wheel from that sdist.
   `uv.lock` controls runtime dependencies; `.github/build-constraints.txt` pins
   the isolated build backend. `scripts/verify_release.py` compares package bytes
   to the checkout, checks all version declarations, and installs the exact wheel
   into a disposable virtual environment with hash-locked runtime dependencies.
   CLI help/version, dashboard assets, and offline bootstrap are exercised
   outside the checkout with network and subprocess access blocked during probes.
4. The same job generates the Homebrew formula and a checksum manifest. The five
   assets are `podgrove-X.Y.Z.tar.gz`, `podgrove-X.Y.Z-py3-none-any.whl`,
   `podgrove.rb`, `release-manifest.json`, and `SHA256SUMS`. The manifest records the
   source commit, package/formula hashes, and installed-package checks.
5. The publication job downloads those exact workflow artifacts, uploads missing
   assets without `--clobber`, downloads them again to compare SHA256 hashes,
   verifies the tag still identifies the expected commit, and publishes the draft.
   A conflicting existing asset or mismatched recovery artifact stops the job.
6. If the tap is configured, a separate job tests the formula on a disposable
   macOS runner and opens a tap PR. Publication does not auto-merge that PR.

This chaining deliberately avoids a separate `release: published` workflow:
releases created with `GITHUB_TOKEN` do not start another workflow. Draft-first
publication also supports [GitHub's immutable release model](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases).

## Homebrew setup

Create `podgrove/homebrew-tap` as a public repository with a default branch and an
initial commit. Its layout is intentionally small:

```text
homebrew-tap/
  Formula/
    podgrove.rb        # generated by a verified Podgrove release
  README.md
  LICENSE
  .github/workflows/  # tap CI for future edits, if configured
```

A tap README should describe `brew install podgrove/tap/podgrove`, `brew upgrade
podgrove`, and the separate prerequisites: `kubectl`, Docker CLI with Compose v2,
and an explicitly configured Kubernetes context/namespace. Installing the Python
CLI does not configure cluster RBAC or start a local Docker daemon.

In `podgrove/podgrove`, create the `homebrew` environment and add its
`HOMEBREW_TAP_TOKEN` secret: a fine-grained token scoped **only** to the tap, with
contents and pull-request write permissions. Set the repository variable
`HOMEBREW_TAP_REPOSITORY=podgrove/homebrew-tap` only after that repository and secret
exist. The tap token is never used by source PR tests or stored in Git remotes.
A GitHub App installation token with equivalent narrow permissions can replace
this secret if the workflow is extended to mint it immediately before checkout.

The formula generator traverses the runtime dependency closure in `uv.lock`,
including conditional backports conservatively. It excludes test/browser extras,
rejects missing/ambiguous/private dependency sources, and includes every selected
PyPI source URL with SHA256. The current closure has seven packages. `rpds-py`
requires Rust at build time, so the formula declares it. Python is installed in
Homebrew's private `libexec` virtual environment following
[Homebrew's Python packaging guidance](https://docs.brew.sh/Python-for-Formula-Authors).

Generated formulae refer to the exact **release sdist asset**, not a moving branch
or GitHub's automatically generated tag archive. No placeholder hash or empty
formula is committed before a release exists. The release job runs:

```sh
brew install --build-from-source podgrove/verification/podgrove
brew test podgrove/verification/podgrove
brew audit --strict podgrove/verification/podgrove
```

`podgrove/verification` is an ephemeral tap created only on the hosted runner.
Homebrew's test block exercises version/help and offline manifest generation; it
never contacts a cluster. A failed Homebrew build blocks the tap PR and leaves the
already verified Python release available. Formula installation is tested on
macOS; Linux Homebrew support is not claimed by that check.

## Local release rehearsal

From a clean public source checkout, with Python 3.11+ and the pinned uv version:

```sh
uv sync --locked --extra test --extra web-test
uv run --no-sync ruff check podgrove scripts tests
uv run --no-sync pytest -q -m 'not cluster and not integration and not browser'
uv run --no-sync playwright install chromium
uv run --no-sync pytest -q -m browser
uv build --no-sources --build-constraint .github/build-constraints.txt --out-dir release-dist
python3 scripts/verify_release.py --dist release-dist --source-sha "$(git rev-parse HEAD)"
```

Use a new output directory for every rehearsal. The verifier requires exactly one
matching sdist and wheel before sealing the directory and refuses existing extra
artifacts. It downloads only hash-locked Python runtime packages into a disposable
venv, never installs globally, and performs no Git mutation or publication.
The source commit argument must identify the actual clean checkout; local dirty
rehearsals do not establish release provenance.

Inspect `release-dist/podgrove.rb`, `release-manifest.json`, and `SHA256SUMS`.
Consumers can download all release assets into a new directory and run
`shasum -a 256 -c SHA256SUMS` (macOS) or `sha256sum -c SHA256SUMS` (Linux).
Checksums detect mismatched bytes; GitHub immutable releases additionally bind the
published assets and tag through the platform's release attestation.

## Recovering a failed publication

Prefer **Re-run failed jobs** on the original Release workflow. It preserves the
successful build's uploaded `verified-release` artifact instead of rebuilding the
same version with potentially different archive bytes. GitHub also supports
[rerunning an individual failed job](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs).

If a fresh recovery run is needed, dispatch **Release** on `main`, supplying both
`tag` and `artifact_run_id` from the original successful build. For example, replace
these illustrative values with the actual release and run ID:

```sh
gh workflow run release.yml --repo podgrove/podgrove --ref main \
  -f tag=v0.2.0 -f artifact_run_id=123456789
```

Recovery verifies the original run belongs to this repository's main-branch
Release push for that exact source commit, retests that commit, and reuses its
artifact. It does not create tags, rebuild assets, or accept another version's
bundle. If all published assets already match, publication is a read-only check
and the tap job can proceed. Missing/different published assets are an error.
Workflow artifacts are retained for 30 days; do not assume an expired artifact can
be reproduced byte-for-byte. Resolve the failure or publish a new version.

A source fix always goes through a new version. Existing users choose when to
upgrade; no release job runs `podgrove up`, `down`, or `reap`, changes an installed
versioned venv, or touches a running worktree.
