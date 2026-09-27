# One committed configuration, multiple worktrees

Use this directory as the layout of an application repository. Set the actual approved context, existing namespace and dynamic StorageClass in `podgrove.yml`, then commit it with the original Compose files. There are no checkout-specific values to replace when another Git worktree is created. The [shared](../shared/podgrove.yml) and [separate namespace](../worktree/podgrove.yml) fragments can also be committed once in an existing application repository.

From each checkout, including its `backend/` subdirectory:

```sh
podgrove validate
podgrove up
podgrove status --json
podgrove env
```

The repository's Git worktree top level determines identity. The committed relative `compose.project_directory` and `compose.files` resolve inside that checkout. Podgrove derives separate engine/PVC names, local state and loopback ports at runtime. Docker allocates the remote application port; `podgrove env` reports the actual local address. A static Compose project name is also safe across different worktrees because each worktree has its own Docker engine.

An administrator must first review and apply the [namespace bootstrap](../../deploy/README.md). In `shared` mode, prepare it once for the configured namespace. To use a namespace per worktree, commit `cluster.namespace_mode: worktree`; the configured namespace becomes a base, and the administrator must create and bootstrap each derived namespace. Run `podgrove bootstrap --output /tmp/podgrove-bootstrap-<unique-name>` from that checkout to render its exact target. The runtime never creates namespaces or grants itself permissions.

Change service resources in the unchanged Compose file and the total engine/PVC budget in `podgrove.yml`. Deliberately different budgets can be committed on different branches, but identity never depends on the branch name or YAML contents. Changing an existing engine/PVC budget is subject to the normal compatibility checks; refresh does not resize it.

This example is intentionally small. In an existing project, keep its own Compose files, interpolation inputs and file order instead of replacing the application with this service. The tests create a disposable real Git repository and two linked worktrees, normalize both with the real Compose CLI, and verify unchanged committed config bytes and distinct runtime identities without starting containers or contacting Kubernetes.
