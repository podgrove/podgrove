# Contributing to Podgrove

Podgrove runs existing Compose projects on isolated remote Docker engines. Preserve Compose semantics, explicit cluster targets, ownership checks and namespace-scoped permissions when changing the runtime.

## Development

Use Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), the Docker CLI/Compose plugin and a disposable browser profile. Local unit and browser tests need no Kubernetes credentials or Docker daemon.

```sh
uv sync --locked --extra test --extra web-test
.venv/bin/playwright install chromium
.venv/bin/ruff check podgrove scripts tests
.venv/bin/pytest -q -m 'not integration and not cluster'
```

On Linux, `playwright install --with-deps chromium` installs browser system dependencies. macOS receiver-shell tests use GNU coreutils (`brew install coreutils`). See [test lanes](tests/README.md) for opt-in local Docker checks. Missing optional tools can cause skips; report them accurately.

Use the development checkout for edits. Install released wheels into separate virtual environments; do not point running shared environments at an editable checkout. Coordinate supervisor upgrades with the owners of active worktrees.

## Documentation website

The [public documentation](https://podgrove.github.io/podgrove/) is built from the existing Markdown guides. Edit those source files so GitHub and the website stay consistent. The [website guide](site/README.md) covers local preview, link and browser checks, and GitHub Pages deployment.

## Changes and reviews

Create a focused branch, explain the user-visible problem and resulting behavior, and include the checks you actually ran. Add regression coverage for changed runtime behavior. Update README/configuration/schema/examples together when adding a configuration key. Keep generated manifests generic and namespace-scoped.

Never run integration or cleanup commands against another person's cluster or stacks. A successful mocked test is not live Kubernetes acceptance. Do not include kubeconfigs, tokens, application secrets, private issue exports, internal rollout notes or generated evidence in a contribution.

## Commit messages and releases

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/): `feat:` for features, `fix:` for fixes, and `docs:`, `test:`, `refactor:` or `chore:` for other changes. Mark breaking behavior with `!` and a `BREAKING CHANGE:` explanation. Release Please uses these messages to prepare version and changelog updates.

The maintainer reviews release changes and validates the exact package before publication. See [the release runbook](docs/releasing.md) for GitHub assets, checksums and Homebrew tap updates. Do not upload packages or move release tags from a contribution branch.

## Issues and security reports

Use [GitHub issues](https://github.com/podgrove/podgrove/issues) for reproducible bugs and feature requests. Include version, OS, sanitized configuration and expected/actual behavior. Follow [SECURITY.md](SECURITY.md) for vulnerabilities; avoid posting credentials or sensitive logs in public issues.
