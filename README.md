<p align="center"><img src="docs/assets/logo.svg" width="64" height="64" alt="Podgrove pods logo"></p>

# Podgrove

**Your Compose stack, a separate environment for every worktree.**

Working on several branches often means running several copies of the same stack: databases, queues, builds and application services competing for your laptop’s memory and ports. Sharing one development stack creates a different problem: one person’s changes or test data can interrupt another’s work.

Podgrove was built to give each worktree its own environment while keeping the Docker Compose workflow you already use. It runs a separate Docker engine in Kubernetes for each worktree, syncs your local source, and brings service endpoints back to `localhost`. Your editor and test commands stay local; the stack runs on the cluster.

Services, volumes, networks, healthchecks and watch rules stay in your existing Compose files. You add a `podgrove.yml` for the cluster target and runtime settings. Each worktree gets its own engine and persistent volume, even when several share a namespace.

- Run concurrent worktrees without sharing databases or Docker networks.
- Keep data across reconnects and refreshes; choose CPU, memory and storage per worktree.
- Inspect services, endpoints, storage and live logs in a read-only browser dashboard.

[How it works](docs/how-it-works.md) · [Design choices](docs/why-not.md) · [Known limits](docs/known-limits.md)

## Install

You need macOS or Linux, Python 3.11+, `uv`, the Docker CLI with a recent Compose plugin, and `kubectl`. Normal use needs no local Docker daemon. The release download below also uses the GitHub CLI.

Install a verified release into a versioned environment, separate from a development checkout:

```sh
git clone --branch v0.2.7 https://github.com/podgrove/podgrove.git
cd podgrove
PODGROVE_RELEASE_DIR="$(mktemp -d)"
gh release download v0.2.7 --repo podgrove/podgrove --dir "$PODGROVE_RELEASE_DIR"
PODGROVE_RELEASE_SHA="$(git rev-parse HEAD)"
python3 scripts/install_release.py --dist "$PODGROVE_RELEASE_DIR" \
  --tag v0.2.7 --source-sha "$PODGROVE_RELEASE_SHA"
export PODGROVE_BIN="$HOME/.local/share/podgrove/current/bin/podgrove"
"$PODGROVE_BIN" --version
```

The installer checks the release identity, asset hashes, locked dependencies and installed package before switching `current`. Keep versioned installations available while their sessions run. For source installation, exact version pinning and coordinated upgrades, see [installation details](docs/getting-started.md#install). **Homebrew installation is not available yet.**

## Start your first worktree

Use an **approved development cluster and existing namespace**. A platform administrator must prepare namespace-scoped access, privileged-engine admission, dynamic storage and NetworkPolicy support. Podgrove does not create namespaces or change cluster-wide settings. Privileged engines share their node’s kernel; this is not a hardened boundary for untrusted tenants. See the [administrator guide](deploy/README.md).

In your **application worktree**, keep the original Compose files and create `podgrove.yml`:

```yaml
version: 1
cluster:
  context: your-development-context
  namespace: your-development-namespace
  namespace_mode: shared
  storage_class: your-delete-storage-class
compose:
  files: [compose.yaml]
```

Replace every example target with approved values and `compose.yaml` with your project’s actual files in their original order. Preserve any profiles, interpolation env file and project name too; the [Compose adoption guide](docs/compose-migration.md) explains the mapping. A missing namespace is an error, and Podgrove never uses the current kube context as an implicit target.

From that application worktree, generate a new folder of bootstrap manifests:

```sh
"$PODGROVE_BIN" bootstrap --output /existing/parent/new-bootstrap-folder
```

Generation is offline. Have the administrator review the folder, prepare authentication, and run the printed apply command against the **same cluster and exact namespace**. Manifests contain their namespace; changing kubectl’s current namespace cannot retarget them. This setup is needed once per shared namespace, or for each derived worktree namespace. [Full setup and upgrade instructions](docs/getting-started.md#configure-the-target-and-prepare-access).

After setup is complete, validate and start from the application worktree:

```sh
"$PODGROVE_BIN" validate
"$PODGROVE_BIN" up --dry-run --json
"$PODGROVE_BIN" doctor
"$PODGROVE_BIN" up
"$PODGROVE_BIN" status --json
"$PODGROVE_BIN" web
```

`up` starts the stack and prints localhost endpoints. A background session keeps source sync and forwarding running; `web` opens the read-only dashboard using the same YAML target. Run the application’s own tests against those endpoints and verify a source edit reaches the remote stack before calling adoption complete. [First-run acceptance checklist](docs/compose-migration.md#4-verify-the-adopted-project).

## Shared namespace or one per worktree

Both modes give each worktree a separate engine and PVC. Compose services run inside that engine; they are not individual Kubernetes Deployments.

| `cluster.namespace_mode` | Where environments run | Administrator setup |
| --- | --- | --- |
| `shared` (default) | Multiple worktrees in the configured namespace | Bootstrap once for that namespace |
| `worktree` | One derived `<base>-wt-<worktree-id>` namespace per worktree | Prepare and bootstrap each derived namespace |

Both require `cluster.namespace`. Node placement is independent: shared nodes are the default; a tainted pool is optional. See [namespace examples](docs/getting-started.md#namespace-modes) and [node placement](docs/configuration.md#node-placement).

## Everyday use

Run commands from the application worktree or a directory inside it. Podgrove uses the Git checkout root as its identity and discovers `podgrove.yml` within that checkout.

```sh
"$PODGROVE_BIN" env --json                 # Ready localhost endpoints
"$PODGROVE_BIN" logs --follow api         # Replace api with your service
"$PODGROVE_BIN" up --refresh              # Rerun Compose; retain engine/PVC data
```

Refresh can interrupt connections, so coordinate it with anyone using the worktree. **`down` deletes that environment and its PVC data**, while leaving the namespace and bootstrap access in place. Idle expiry and an explicitly linked GitLab MR can also trigger cleanup; application traffic and dashboard viewing do not extend the idle TTL. See [lifecycle and cleanup](docs/operations.md).

## Documentation

| I want to… | Guide |
| --- | --- |
| Install, upgrade or prepare namespace access | [Getting started](docs/getting-started.md) · [Administrator setup](deploy/README.md) |
| Adopt an existing Compose project, including with an agent | [Compose migration and acceptance](docs/compose-migration.md) |
| Configure resources, PVCs, targets, paths or forwarding | [Configuration reference](docs/configuration.md) · [Schema](schema/podgrove-v1.schema.json) |
| Reconnect, inspect logs, manage networking or clean up | [Operations](docs/operations.md) · [Known limits](docs/known-limits.md) |
| Use the browser dashboard | [Dashboard guide](docs/web.md) |
| Work across backend and frontend repositories | [Development workflows](docs/dogfood-workflows.md) |
| Check behavior or contribute | [Verification](docs/verification.md) · [Contributing](CONTRIBUTING.md) · [Release runbook](docs/releasing.md) |

Configuration examples: [shared namespace](examples/shared/podgrove.yml), [per-worktree namespace](examples/worktree/podgrove.yml), [portable worktrees](examples/portable/README.md), [custom resources](examples/resources/podgrove.yml), [network exclusions](examples/network/podgrove.yml), and [dashboard only](examples/dashboard/podgrove.yml).

[MIT licensed](LICENSE). Please report security concerns through [SECURITY.md](SECURITY.md). The project’s logo is documented in [branding](docs/branding.md).
