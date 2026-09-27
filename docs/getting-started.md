# Getting started

Install a stable Podgrove release, save the approved target in your application worktree, and have its namespace access prepared before starting services. For a first adoption, also follow the [Compose migration guide](compose-migration.md).

## Install

Requirements: macOS or Linux, Python 3.11 or newer, `uv` or pip, the Docker CLI with a recent Compose plugin, and `kubectl` with working authentication for the approved cluster. Compose must support `config --format json --no-env-resolution`, `watch --no-up` when watch rules are used, and the features present in your Compose files. Normal remote use needs no local Docker daemon. This repository's opt-in Docker integration tests do need one; Docker Desktop is not required.

### Install a verified release

For shared automation, install a published release into its own versioned environment.
The installer requires Python 3.11+, `uv`, and a complete release bundle; these download
commands also use the GitHub CLI. Choose a version from [Releases](https://github.com/podgrove/podgrove/releases).
From a checkout of that tag, download its five assets into a new directory:

```sh
git clone --branch v0.2.3 https://github.com/podgrove/podgrove.git
cd podgrove
PODGROVE_RELEASE_DIR="$(mktemp -d)"
gh release download v0.2.3 --repo podgrove/podgrove --dir "$PODGROVE_RELEASE_DIR"
PODGROVE_RELEASE_SHA="$(git rev-parse HEAD)"
python3 scripts/install_release.py --dist "$PODGROVE_RELEASE_DIR" \
  --tag v0.2.3 --source-sha "$PODGROVE_RELEASE_SHA"
export PODGROVE_BIN="$HOME/.local/share/podgrove/current/bin/podgrove"
"$PODGROVE_BIN" --version
```

The installer verifies asset hashes and the expected tag/commit, uses the release's
locked dependency hashes, checks the installed CLI and dashboard assets, and renders
bootstrap manifests offline. It creates `~/.local/share/podgrove/<version>-<commit>/venv`
and an `installation.json` receipt, then atomically switches `current` after every
check passes. The installation never reads kubeconfig or contacts a cluster.
Checksums establish integrity; obtain the bundle and checkout from the trusted repository.

An existing version directory is never overwritten, even if an earlier installation
was incomplete. Failed installation checks preserve the previous `current` selection.
Use `--install-root /absolute/path` for a separate installation, `--python /path/to/python`
to choose its interpreter, or `--offline` when all locked dependencies are already
cached by `uv`. Keep installed version directories intact while their sessions run.

To upgrade, repeat with the new tag and a fresh download directory. Updating `current`
affects future commands; existing supervisors retain their original interpreter.
Coordinate any `up --refresh` with the worktree's owner because it restarts local
connections and reruns Compose. Do not refresh another seat's stack during an upgrade.
For an automation run that must remain on one version while `current` changes, set
`PODGROVE_BIN` to the receipt's exact `executable` path.

### Install from a source checkout

Clone the source if needed, install an isolated copy of the package, then run it from the application worktree:

```sh
git clone https://github.com/podgrove/podgrove.git
cd podgrove
uv tool install .
export PODGROVE_BIN="$(command -v podgrove)"
"$PODGROVE_BIN" --version
```

If the executable is not on your PATH, run `uv tool update-shell` and open a new shell. Pip users can create a separate virtual environment and run `python -m pip install /absolute/path/to/podgrove` in it. Avoid editable installations for shared automation: source edits must not change the runtime used by other worktrees. [Release pinning and upgrades](releasing.md).

### Homebrew distribution

The planned install command is:

```sh
brew install podgrove/tap/podgrove
```

**The Homebrew tap is still being prepared. This command is not available yet.** The [release runbook](releasing.md) explains how a verified release becomes a reviewed formula update. Use the verified release installer or source installation above.

`PODGROVE_BIN` should point to the absolute installed executable. Keep its environment available while worktrees run: background sessions use the interpreter that launched `up`, with Python import isolation so the caller’s directory and ambient import paths cannot select another checkout. Coordinate upgrades and `up --refresh` with active users. Development requires the documented [editable installation](verification.md#local-checks); setting `PYTHONPATH` to uninstalled source does not select the supervisor runtime.

## Configure the target and prepare access

An administrator must provide an **existing approved namespace**, a kubeconfig identity with the required namespace-scoped permissions, admission for a privileged Docker engine, approved dynamic storage, and a NetworkPolicy-capable cluster. Shared node placement still requires a privileged engine. Podgrove uses only namespaced Kubernetes APIs: it never reads or mutates Namespace, node, StorageClass or PV objects, and it never installs cluster RBAC or admission policies.

Save the target in the **application worktree's `podgrove.yml`**. Replace these example values with your approved kubeconfig context, existing development namespace, and StorageClass:

```yaml
cluster:
  context: your-development-context
  namespace: your-development-namespace
  namespace_mode: shared
  storage_class: your-delete-storage-class
```

There is no default namespace or testing-namespace allowlist. A missing namespace stops the command with `No namespace provided` before Compose, cluster access, or browser startup. `namespace_mode` defaults to `shared`; choose `worktree` explicitly for a derived namespace per worktree, as described under [Namespace modes](getting-started.md#namespace-modes). The platform must prepare that actual derived namespace too.

From that same worktree, generate a dedicated folder of concrete Kubernetes manifests:

```sh
"$PODGROVE_BIN" bootstrap --output /path/to/new-bootstrap-folder
```

The parent directory must exist and the output folder must be new. Generation is offline and needs no Compose file or cluster access. **It cannot inspect the namespace, existing same-name objects, or other workloads.** It prints the actual target and apply command. The five YAML files contain nine namespaced objects: a provisioning ConfigMap, a deny NetworkPolicy selecting only Podgrove-managed Pods, two ServiceAccounts, two Roles and three RoleBindings. It emits no Namespace, ClusterRole, ClusterRoleBinding, admission policy, ResourceQuota or LimitRange. Bootstrap does not require or inspect the StorageClass. See the [administrator guide](../deploy/README.md) for exact permissions and installation review.

By default the human binding names the Kubernetes group `podgrove-developers`. The administrator must map the developer's authenticated kubeconfig identity to that group, or generate with `--developer-group your-approved-group`. Applying RBAC does not create authentication or narrow a more privileged identity. Alternatively, the administrator can supply a separate private kubeconfig for the generated `podgrove-client` ServiceAccount, with the intended API server/CA and an approved short-lived credential. Select that kubeconfig using the normal `KUBECONFIG` mechanism and its context in YAML; do not copy administrator credentials or add cluster grants to make checks pass. [Authentication setup](../deploy/README.md#authentication-and-the-selected-kubeconfig).

After checking resource-name collisions and approving the trust relationship with existing workloads, an authorized namespace administrator applies that folder to the **same cluster**:

```sh
kubectl --context your-admin-context --namespace your-development-namespace apply -f /path/to/new-bootstrap-folder
```

The manifests contain explicit namespaces; changing kubectl's current namespace cannot retarget them. Generate a fresh folder for a different target. Keep the applied folder for later retirement. The named `podgrove-bootstrap` ConfigMap records shared/worktree mode and the worktree identity where applicable; runtime validates this marker rather than querying or adopting a Namespace.

An approved existing `default` is supported; bootstrap refuses reserved `kube-*` targets. Its policy leaves unrelated/unlabelled CI Pods unselected. However, Pod creation and workload-management rights remain namespace-wide, and CLI ownership checks are not a hardened tenant boundary. Existing privileged-container restrictions, resource quotas, limits and networking rules must be reviewed by the platform owner; Podgrove does not alter them. The administrator also verifies the selected StorageClass and its reclaim behavior. Namespace-scoped Podgrove cannot prove backing-volume deletion, even after a PVC is gone.

**Upgrading an existing installation:** regenerate, review and apply the current namespaced bundle, including its `podgrove-bootstrap` marker, before `doctor` or `up`. Older manual SA/Role subsets lack that required marker. After installation, run `up --refresh` once for an existing worktree to load the updated supervisor while retaining its engine/PVC data; this re-runs Compose, so coordinate it with active tests. Already-running old supervisors do not reload Python code automatically. `down` and retained diagnostics remain available without the marker. If a platform previously installed broader cluster grants/admission/quota controls from an old bundle, have its administrator review their retirement separately; the new bundle neither emits nor removes those objects.

## Namespace modes

Podgrove supports both one namespace per worktree and multiple worktrees in a single namespace:

| Mode | Configuration for a new environment | Resources and cleanup |
| --- | --- | --- |
| Shared namespace | Set `cluster.namespace` to your namespace and use `cluster.namespace_mode: shared` (the default mode). | Have the platform prepare that namespace, then bootstrap once for it. Multiple worktrees get separate engine Pods, StatefulSets, and PVCs inside it. `down` retains the namespace and bootstrap resources. |
| Namespace per worktree | Set `cluster.namespace` to a base name and `cluster.namespace_mode: worktree`. | The target is `<base>-wt-<worktree-id>`; long bases are shortened with a hash. Have the platform prepare each derived namespace, then generate/apply bootstrap from **each worktree**, using a separate output folder for each. `up`, `doctor`, `web`, and cleanup resolve the same target. `down` retains the existing namespace and its bootstrap access setup. |

In either mode, each worktree's Compose services run inside its own Docker engine, with separate networks and named volumes. Compose services are not individual Kubernetes Deployments. Sharing a namespace does not mean sharing an engine or PVC.

For example, keep the context and StorageClass fields from the setup above and change the namespace settings to:

```yaml
cluster:
  context: your-development-context
  namespace: my-team
  namespace_mode: worktree
  storage_class: your-delete-storage-class
```

Run `bootstrap --output /path/to/worktree-a-bootstrap` from worktree A and generate a different folder from worktree B. An administrator first prepares each printed actual namespace through the platform process, then applies its folder to the selected cluster. Applying A's folder does not grant access to B's namespace. The CLI does not grant itself namespace-creation or RBAC-administration permissions. In shared mode, all worktrees using the same target reuse one installation.

For two existing application worktrees with `namespace_mode: worktree` in their respective `podgrove.yml` files, generate from each exact root. Replace the paths, and keep the output outside both worktrees:

```sh
mkdir -p /path/to/platform-manifests
"$PODGROVE_BIN" bootstrap --project-directory /path/to/worktree-a --output /path/to/platform-manifests/worktree-a
"$PODGROVE_BIN" bootstrap --project-directory /path/to/worktree-b --output /path/to/platform-manifests/worktree-b
```

The administrator prepares the two printed namespaces, reviews each folder, then applies them on the configured cluster (each manifest names its exact namespace):

```sh
kubectl --context your-admin-context apply -f /path/to/platform-manifests/worktree-a
kubectl --context your-admin-context apply -f /path/to/platform-manifests/worktree-b
```

Each developer then runs the validation/startup sequence from their corresponding root; for example, `"$PODGROVE_BIN" up --project-directory /path/to/worktree-a`. Namespace identity depends on that resolved local root: generate the bundle using the same root that will run `up`, even when a different administrator applies the files.

Each application worktree reads its own configuration; no developer target ships with Podgrove. Missing `cluster.namespace` is an error in both modes; omitting it never enables per-worktree namespaces.

Namespace mode is independent of `node_mode: shared` or `tainted`, which controls node placement. Namespace names follow Kubernetes DNS-label syntax. Changing namespace or namespace mode requires removing the old environment before starting the new one; `down` deletes its PVC data. To retire a whole platform installation, first stop all its worktrees, then have an administrator run `kubectl --context your-admin-context delete --ignore-not-found -f /path/to/its-bootstrap-folder`. This removes only the generated namespaced access/marker/baseline objects; it never deletes the Namespace or unrelated workloads. Review all users before retiring a shared installation.
