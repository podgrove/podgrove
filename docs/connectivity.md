# Connecting development environments

Compose services within one worktree already communicate over their Compose networks. Connections to your laptop or another worktree must be declared in `podgrove.yml`. These features use the configured cluster and namespace; they never create nodes, namespaces or cluster-wide permissions.

## Reach a service on your laptop

If a local API listens on `127.0.0.1:18080`, add:

```yaml
reverse:
  - local_port: 18080
    remote_port: 8080
```

Run `podgrove up`. Compose containers can now use `http://host.docker.internal:8080`. For a web development proxy, set `DEV_PROXY_TARGET` to that address in your existing Compose environment settings.

`local_port` can also be another Podgrove environment's printed localhost forward. In that case, keep that environment's forwarding session running. Use `podgrove env --json` to find its current port.

`remote_port` defaults to `local_port`; engine listeners must use ports 1024–65535, excluding Docker ports 2375 and 2376. `local_host` defaults to `127.0.0.1`; the only alternative is `::1`. Ports are TCP, with at most 32 distinct engine listeners. Published Compose ports must not overlap reverse listeners.

The supervisor opens an authenticated Kubernetes exec channel and a restricted helper container inside the engine. That helper binds the Docker bridge gateway, not the engine Pod's external interface. No incoming connection to your laptop or manual `kubectl port-forward` is required. A generated Compose override adds the host alias without changing your source files. The helper has no filesystem or Docker socket mounts and no service-account token; it shares the engine's network namespace and consumes that engine's configured resource budget.

The local service must be running. Reverse forwarding stops when its local supervisor stops or your laptop is offline. A broken channel reconnects within a bounded retry budget; interrupted TCP streams close and are never replayed. Applications should reconnect their own requests. `status --json` includes `connectivity_status` and channel errors.

## Reach another engine in the same namespace

First start the target environment using this release, then copy its `identity` from `podgrove status --json`. Its requested Compose service must publish a TCP port on `0.0.0.0`; target-only declarations such as `ports: ["8080"]` are supported.

In the source worktree:

```yaml
connect:
  - name: api
    environment: abcdef123456
    service: gateway
    port: 8080
```

Replace the example identity with the target's real 12-character identity. After `podgrove up`, use `http://api.podgrove:8080` from the source's Compose containers. `port` is the target service's container port, even when Docker chooses a different published port. A link does not require the target's local application port-forward to remain open.

Each link creates a source-owned namespaced Service and two narrow NetworkPolicies: source egress to the target engine's published TCP port, and target ingress from that exact source engine. Other engines, ports and namespaces receive no allowance. The base isolation policies remain in place. Cluster DNS and permitted public HTTP/HTTPS egress keep their existing rules.

Names are unique DNS labels exposed as `<name>.podgrove`; at most 32 links are accepted. Self-links, namespace/context overrides, unpublished or loopback-only target ports, ambiguous service containers and conflicting Compose host aliases are refused. Targets from older releases need one `up` to record their Compose project before they can be linked.

The link follows replacement Pods belonging to the same target StatefulSet. The source supervisor periodically verifies the recorded Compose project, service and actual published port, and updates the narrow rules when a Docker-assigned port changes. Failed verification revokes link policies when the API is reachable. A replaced controller or Service IP requires explicit reconciliation; the problem is reported instead of adopting another environment. These checks are periodic, not per-packet identity checks.

Remove the declaration and run `up` to remove the allowance. `down` and scoped reaping remove source-owned link resources without deleting the target environment. Existing declared links can remain usable while the source laptop is disconnected, but automatic verification and port updates require its supervisor. NetworkPolicies are additive and CNI enforcement varies; an administrator must verify the real packet path and ensure other policies do not reopen it.

## Choosing between them

Use `reverse` for a natively running local service or a localhost endpoint you already use. Use `connect` for direct communication between two engines in the same namespace. Set your application's proxy target to the documented stable address; Podgrove does not change application environment variables automatically.
