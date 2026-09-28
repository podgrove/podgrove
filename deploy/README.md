# Namespace-scoped bootstrap

`podgrove bootstrap` writes a concrete installation folder for **one existing, operator-approved namespace**. Generation is offline: it does not run kubectl, inspect workloads, apply resources, or resolve Compose. Every generated object is namespaced. Podgrove does not create, read, label, update or delete Namespace objects, and it installs no ClusterRole, ClusterRoleBinding, admission policy, ResourceQuota or LimitRange.

**Review the target and existing object names before applying.** Offline generation cannot determine whether the namespace exists, whether its occupants trust each other, or whether an existing `podgrove-*` object belongs to another installation. The deny policy selects only Pods labelled `app.kubernetes.io/managed-by: podgrove`; it does not select unrelated or unlabelled CI Pods. Existing platform policies still apply, and namespace-scoped Pod creation privileges are a broad trust grant.

## Generate, review and apply

Save the application worktree's approved target in `podgrove.yml`:

```yaml
cluster:
  context: team-development
  namespace: team-development
  namespace_mode: shared
  storage_class: approved-delete-storage
```

The kubeconfig context must already authenticate to the intended cluster. The namespace must already exist; arrange its creation through the platform's normal process if needed. `default` is permitted when approved, while reserved `kube-*` targets are refused by bootstrap. Shared mode uses the exact configured namespace. Worktree mode derives `<base>-wt-<identity>` and requires that actual namespace to be prepared before installation.

`cluster.storage_class` is an administrator-approved class for engine startup. Bootstrap does not require it, create it, or grant access to it. The administrator must verify that dynamic provisioning and the desired backing-storage cleanup policy are appropriate. Podgrove cannot inspect StorageClasses, PVs or cloud volumes, and PVC binding/deletion does not prove a backing volume was deleted.

Keep generated manifests outside the mirrored worktree. Their parent directory must already exist without symlink components, and the output folder must be new; even an existing empty folder is refused. Retain the reviewed files as the installation and retirement record.

```sh
mkdir -p ../podgrove-platform
podgrove bootstrap --output ../podgrove-platform/team-development
```

The command reads the configured target without opening Compose or env files. `--context`, `--namespace`, `--namespace-mode` and `--developer-group` override their corresponding choices; the default developer group is `podgrove-developers`. The compatibility `--storage-class` option does not change this namespace-only bundle. No node-reader installation option is provided.

Generation creates a private output directory and checks directory/file identities while writing. Concurrent replacements or edits cause failure, and changed content is preserved for review. A reported partial output requires a fresh output path on retry. This protects against accidental concurrent changes, not a hostile process with the same filesystem privileges.

The folder contains five YAML files and nine objects:

| File | Objects and purpose |
| --- | --- |
| `00-provisioning-marker.yaml` | One ConfigMap recording namespace mode and, for worktree mode, its identity |
| `05-network-isolation.yaml` | One deny policy selecting only Podgrove-managed Pods |
| `10-client-rbac.yaml` | Client ServiceAccount, Role and RoleBinding |
| `30-developer-bindings.yaml` | One RoleBinding for the approved human group |
| `40-reaper-rbac.yaml` | Separate cleanup ServiceAccount, Role and RoleBinding |

All resource names are local to the selected namespace. Before installation, review any same-name objects, approved human/group membership, the generated rules, and existing admission/network policies. A privileged Docker engine must already be allowed by the platform; this bundle supplies no admission exception and cannot override an existing denial. Existing quotas and limit ranges may also reject the chosen engine size. Podgrove neither changes those policies nor claims to reserve cluster capacity in advance.

An authorized namespace administrator applies the reviewed folder on the same cluster:

```sh
kubectl --context platform-admin --namespace team-development apply -f ../podgrove-platform/team-development/
```

The manifest namespaces are explicit. Changing kubectl's current namespace does not retarget the files; generate a fresh folder for another target. Applying the files changes only the listed objects, but an existing same-name Role or RoleBinding can affect current users, so installation still requires review.

Offline tests check these grants and generated manifests. Validate the selected cluster separately using the intended namespace-scoped identity; see [verification](../docs/verification.md).

For an upgrade, regenerate and apply the current namespaced bundle after reviewing existing names. The provisioning marker is required even if an earlier manually applied SA/Role subset already works. After installation, coordinate `up --refresh` for existing worktrees to restart their old supervisors with the new recovery code; their engine/PVC data is retained, but Compose runs again. Cleanup and retained diagnostics do not require the marker. Older cluster grants/admission/quota objects, if separately installed, require administrator review outside this new bundle; generation never deletes them.

## Authentication and the selected kubeconfig

RoleBindings authorize an identity that Kubernetes has already authenticated. They do not create a kubeconfig, map an EKS/IAM user to a group, or narrow permissions an administrator identity already has. Use either:

- An approved human kubeconfig whose authenticated group matches `podgrove-developers`, or the chosen `--developer-group`. The platform owner arranges that mapping outside Podgrove.
- A separate kubeconfig authenticating as the generated `podgrove-client` ServiceAccount in this namespace. An authorized administrator obtains a short-lived token for that account and supplies the selected cluster's API-server URL and CA. Keep the kubeconfig private and separate from administrator credentials, set its context namespace, and renew the token through the platform's approved workflow. Do not copy an administrator user entry into it or grant broader cloud access to make a check pass.

For example, an authorized administrator can request a temporary credential for the already installed account:

```sh
kubectl --context platform-admin --namespace team-development create token podgrove-client --duration=1h
```

That command prints a credential: deliver it through an approved secure channel, not logs or repository files. Podgrove itself never requests ServiceAccount tokens. Token expiry and any approved alternative authentication workflow remain the administrator's responsibility.

Select the resulting context in `cluster.context`. `kubectl` uses the normal `KUBECONFIG` selection, including a separately supplied kubeconfig file. Podgrove explicitly passes the chosen context and namespace to its namespaced Kubernetes commands; it never falls back to the current context or edits your kubeconfig.

Check with the actual developer identity:

```sh
kubectl --context team-development --namespace team-development auth can-i get configmaps/podgrove-bootstrap
kubectl --context team-development --namespace team-development auth can-i create pods/exec
kubectl --context team-development --namespace team-development auth can-i get roles.rbac.authorization.k8s.io/podgrove-client
podgrove doctor
podgrove up
```

The three named checks should return `yes`. Doctor checks required namespaced permissions, the provisioning marker, and the engine's server-side admission path or existing compatible engine. It does not list nodes, inspect Namespace/StorageClass/PV objects, or establish that the application works. Scheduling outcomes are reported through the owned Pod's status. Tainted placement uses a configured selector/toleration without adding node-read grants; the platform owner must approve those values.

## Provisioning marker and retained setup

Runtime validates only the exact ConfigMap `podgrove-bootstrap`, rather than reading or changing the Namespace. Its labels are `app.kubernetes.io/managed-by: podgrove` and `podgrove.dev/component: bootstrap`. Its data contains `version: "1"` and `namespace_mode: shared|worktree`; worktree mode also records the exact 12-character environment identity as `environment`. A shared marker has no environment field. The marker carries **no environment ownership label**, so ordinary environment cleanup retains it.

A missing, foreign or mismatched marker blocks startup with instructions to regenerate and apply the reviewed namespace-scoped bundle. A marker is an installation contract, not proof of effective authorization, safe admission, or exclusive namespace ownership. It can be changed by identities allowed to modify ConfigMaps.

Shared mode installs one bundle for all authorized worktrees using that namespace. Worktree mode needs a separate existing namespace and bundle for each worktree; generate from the exact root later used by `up`. Identity depends on that resolved root. A worktree marker blocks another identity from accidentally using the same setup.

`down` and reaping delete only environment resources selected by both Podgrove's management label and environment identity. They retain the namespace, provisioning marker, baseline policy and bootstrap RBAC. This applies to cleanup of older recorded namespace modes too; Podgrove never deletes a Namespace.

To retire the installation, first remove every Podgrove environment using it and review the effects on all users. An authorized namespace administrator may then delete the exact retained bundle:

```sh
kubectl --context platform-admin --namespace team-development delete --ignore-not-found -f ../podgrove-platform/team-development/
```

This removes only the generated namespaced setup objects. It does not delete the Namespace or unrelated workloads. It removes Podgrove access and its baseline policy for everyone using that installation, so it is separate from routine worktree cleanup.

## Upgrading engine protection permissions

Regenerate the bootstrap bundle after upgrading to the release that adds engine disruption protection. Have the namespace administrator review and apply the client and reaper RBAC files: the client needs namespaced `policy/poddisruptionbudgets` lifecycle verbs, and the reaper needs get/list/watch/delete. Existing engines receive annotations and a per-engine PDB on their next `up`, without a Pod restart. A denied PDB preflight stops deployment before allocating an engine or PVC.

## Permission and isolation limits

The client Role grants namespace-wide rights on Pods, StatefulSets, Services, PVCs, ConfigMaps, NetworkPolicies and PodDisruptionBudgets. Docker API streams need `pods/exec`; application tunnels need `pods/portforward`. Both permit GET and CREATE for supported streaming protocols. GET-only `pods/log` lets the dashboard read the owned engine's logs. The client also receives exact named GETs for the two ServiceAccounts, two Roles and three RoleBindings shown by Configuration. There are no node, Namespace, StorageClass, PV, cluster-RBAC, Secrets, token, bind or escalate grants.

The Configuration page makes eight exact namespaced metadata reads: the provisioning ConfigMap and those seven SA/RBAC objects. It reports the selected namespace string and observed marker; it does not read Namespace metadata. Missing and inaccessible declarations are distinguished. Declarations are not effective-permission checks. The reaper receives none of the dashboard-specific SA/RBAC reads or streaming/log permissions.

RBAC does not restrict these workload rights by ownership labels. Creating Pods can expose other namespace resources or ServiceAccounts, and direct kubectl use can bypass Podgrove's ownership checks. Normal cleanup refuses foreign resources, but the generated credentials are not a hardened hostile-tenant boundary. Approve the trust relationship with existing CI workloads before sharing a namespace. Engine Pods disable token automounting and receive no Kubernetes credentials; their privileged Docker daemon still shares the node kernel.

The baseline policy selects only `app.kubernetes.io/managed-by: podgrove` Pods. It denies both ingress and egress unless another matching policy allows traffic. Per-environment policies allow the configured DNS exception and public IPv4 HTTP(S), excluding built-in reserved ranges and administrator-supplied `network.blocked_cidrs`. Add actual protected Pod, Service, node, control-plane and other infrastructure ranges where needed; the offline generator cannot discover them. Unrelated CI Pods are not selected by the baseline. The optional reaper is managed and therefore needs its separate reviewed API/DNS policy.

NetworkPolicy allowances are additive, so another matching allow policy can reopen traffic. The CNI must enforce policy; DNS deployment, Service translation and node-local traffic require platform review. The developer Role can modify policies and the reaper Role can delete them directly, even though normal CLI cleanup retains the baseline. Tamper-resistant enforcement requires platform controls outside these credentials. Review and test actual packet paths before relying on isolation.

Startup requires an explicit administrator-approved StorageClass for a new PVC. Reconnect can reuse the class recorded on the owned PVC. Podgrove checks PVC identity, requested size, access mode, class compatibility and deletion state using only namespaced APIs. It cannot verify reclaim policy, provisioner or backing-volume deletion. Choose a class with the administrator-approved cleanup behavior and have the platform independently verify storage retirement when required. A retained backing volume can continue consuming resources after `down` has removed its PVC.

## Optional scheduled reaper

Bootstrap includes a distinct `podgrove-reaper` ServiceAccount and cleanup-only Role with GET/LIST/WATCH/DELETE on the namespaced cleanup kinds. It cannot create/update workloads, stream into Pods, read Secrets or cluster metadata, or delete a namespace. Deletion grants remain namespace-wide; the reaper's implementation additionally checks management and environment ownership.

The files in [`reaper/`](reaper/) end in `.yaml.example` and are deliberately not an apply-ready folder:

1. Build and publish an image from [`Dockerfile`](Dockerfile), using the repository root as the build context.
2. Copy all three templates into a separate reviewed folder with `.yaml` extensions. Replace every `PODGROVE_NAMESPACE` with the actual existing target namespace, including kubeconfig context and command arguments, and replace the image placeholder.
3. Replace the API destination CIDR and port placeholders in `20-network-policy.yaml` with the narrowest platform-reviewed endpoint ranges/ports. Account for Service translation and verify the DNS selectors. Do not substitute unrestricted Internet access.
4. Review and apply that folder with the authorized namespace administrator on the intended cluster.

The CronJob explicitly uses `reap --all` to evaluate all eligible Podgrove environments in its one namespace. Without `--all`, ordinary local `reap` targets the current worktree. The reaper uses its projected short-lived ServiceAccount token and cluster CA, with automount disabled and read permissions for its non-root process. Replacing that kubeconfig with broader credentials defeats the restriction.

The Pod and API/DNS policy use management and `podgrove.dev/component: reaper` labels, with no environment identity. The allow policy selects the reaper rather than engines and survives ordinary environment cleanup. Without it, the managed-Pod baseline blocks API access even when RBAC is correct. Node-hosted API endpoints may require CNI-specific allowances; administrators must validate that path.

The template grants no general HTTP(S) or GitLab access. Optional MR checks need a separately reviewed destination policy and an explicitly supplied `PODGROVE_GITLAB_TOKEN` with `read_api`; neither is supplied here. An in-cluster reaper cannot infer that another machine's worktree disappeared. Only matching private local state can support that local cleanup proof.
