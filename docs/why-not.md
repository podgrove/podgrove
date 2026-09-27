# Design choices

Podgrove keeps Docker Compose as the application definition. Each worktree gets a separate remote Docker engine so services, networks, named volumes and builds use Compose's own behavior. The tradeoff is one privileged engine per worktree, rather than independent Kubernetes Deployments for every service.

## When this model fits

Use Podgrove when a project already has working Compose development/test files and needs separate remote capacity for concurrent worktrees. A shared namespace can contain several engines without sharing their Docker networks or volumes. Namespace-per-worktree placement is also supported when administrators prepare each target.

Use a different architecture when you need native Kubernetes application deployments, production orchestration, untrusted tenant isolation, hardware passthrough or Docker behavior tied to a developer's laptop. See [known limits](known-limits.md) before adoption.

## Why synchronization and Compose watch both exist

The local Docker client cannot directly bind a laptop directory into a remote engine. Podgrove mirrors declared local files before startup and keeps tracked bind/config/secret sources updated. Compose watch remains responsible for explicitly declared watch actions, such as rebuilds. Neither mechanism invents an application's hot-reload behavior.

## Why targets and resources are explicit

Cluster context and namespace identify where side effects happen, so Podgrove refuses a missing namespace. Shared nodes are the default; optional tainted placement expresses an administrator-approved selector and toleration without reading or modifying node inventory. Resource presets are conveniences, while explicit budgets and PVC capacity let each worktree choose its allocation.

These choices preserve a small local-tool boundary: kubeconfig authenticates the user, namespace-scoped manifests describe the engine, and existing cluster admission, storage and network policy remain authoritative.
