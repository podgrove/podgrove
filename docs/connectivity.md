# Connecting development environments

Compose services within one worktree already communicate over their Compose networks. Connections to your laptop or another worktree must be declared in `podgrove.yml`. Reverse forwards use the configured engine. Pod-to-Pod rules can select peers in explicitly named namespaces of the same cluster; neither feature creates nodes, namespaces or cluster-wide permissions.

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

## Choose a Pod-to-Pod mode

```yaml
network:
  pod_to_pod: disabled
```

`disabled` is the default: Podgrove adds no peer-traffic allowance. Compose services within the same engine and laptop port-forwards keep working. The independent DNS and public-web rules described below remain in effect.

Set `open` to allow any port to or from Pods labelled as Podgrove-managed in the engine's own namespace. List additional exact namespaces in `network.namespaces`; the other side must list yours too. Open never matches every namespace, so a Pod that labels itself in an unlisted namespace gets nothing. Both endpoints must permit a connection; an open source cannot override a disabled destination. Open does not grant access to arbitrary unlabelled Pods, expose a public load balancer, or remove filtered public-egress exclusions.

Use `selected` for explicit server/client rules. Both endpoints must use selected mode and declare matching permissions, each enforced by that environment's own policy. An open/selected pair does not receive a selected grant; use open at both ends or matching selected rules at both ends. A client never writes a server's ingress allowance.

## Connect selected worktrees

Suppose a server worktree is named `apis-checkout` and a client is `web-checkout`. Use the actual stable worktree names reported by Podgrove; these derive from checkout directory names, not branches. Replace the example namespace with the prepared target. The two worktrees may use the same namespace or different approved namespaces in the same cluster.

In the server's original Compose file, publish a fixed TCP port:

```yaml
services:
  api-gateway:
    image: your-api-image
    ports:
      - "0.0.0.0:18080:8080"
```

Keep the application's real image, command and other settings. The application must listen on container port `8080`; this mapping makes it reachable at the **engine's published port `18080`**.

In the server's `podgrove.yml`:

```yaml
network:
  pod_to_pod: selected
  expose:
    - service: api-gateway
      from:
        - namespace: your-development-namespace
          worktree: "web-*"
```

In the client's `podgrove.yml`:

```yaml
network:
  pod_to_pod: selected
  connect:
    - namespace: your-development-namespace
      worktree: "apis-*"
      ports: [18080]
```

The client's rule names the **server namespace**; the server's `from` rule names the **client namespace**. Patterns are case-sensitive worktree-name globs. Omitting `worktree` within `from` allows every managed worktree in that exact namespace. The client must provide a worktree pattern. Namespace globs are not supported.

Validate both worktrees before cluster changes:

```sh
podgrove validate
podgrove up --dry-run --json
```

These checks are offline: they validate local YAML and Compose declarations, but cannot prove the peer exists, consents, is reachable or grants discovery access. `selected` exposure requires an existing enabled service, one replica, and fixed wildcard TCP publications. Target-only, zero, ranged, loopback-only, UDP and ambiguous publications are refused. Docker API ports 2375 and 2376 are reserved. Dynamic publications remain supported for ordinary localhost forwarding outside selected exposure.

Run `up` in both worktrees, then inspect `status --json`. `worktree_name` is the stable name used by patterns. `pod_network_status` reports the mode, state (`ready`, `waiting` or `unavailable`), endpoints and pending matches; an unmatched or inaccessible peer is not a confirmed connection. The server's top-level `peer_endpoints` lists its advertised service, container `target`, engine-published `port`, DNS `host` and convenience `url`. Use that host with port `18080` from the client, for example `http://<reported-peer-dns>:18080`.

The DNS name belongs to the engine's headless Kubernetes Service; it is not the Compose service name, a laptop endpoint, or the old `<alias>.podgrove` host mapping. Reported names currently end in `.svc.cluster.local`: this assumes the cluster uses `cluster.local`. Custom cluster DNS suffixes are not discovered or configurable in this version. The target's local port-forward need not remain open. Podgrove does not change your application's URL environment variables.

Namespaces must already exist. The kubeconfig identity needs `get` and `list` access to Pods and ConfigMaps in every explicitly referenced peer namespace, granted through namespaced RoleBindings. No Namespace reads or cluster-wide permissions are needed. Runtime verifies peer identity, consent and the server's actual published binding before granting selected access. A discovery error is not interpreted as permission to reach a wider set of Pods. Changes reconcile through the local supervisor; inspect status after changing rules or losing discovery access.

## Migrate legacy environment links

The old **top-level** `connect: [{name, environment, service, port}]` feature is replaced by `network.pod_to_pod: selected`. Nonempty legacy declarations are refused with migration guidance. They are not silently translated: the old client created the target's ingress policy, while the new model requires the target's own consent.

Before upgrading a worktree that has old connection resources, coordinate with its owner and save any data that must survive. Use the recorded older Podgrove installation to retire that environment with scoped `down`; **this deletes its PVC and stored data**. The new release refuses retained old connection resources instead of adopting or deleting them automatically. Do not run a broad reaper or remove another worktree's policy.

Configure the server's `network.expose` and the client's `network.connect`, then start both with the new release. Replace old `<alias>.podgrove:<container-port>` application URLs with the reported engine DNS name and its fixed **published** port. Worktrees without legacy links do not need this retirement step merely to use the new modes.

## Network boundaries

Public IPv4 HTTP(S) and scoped cluster DNS have independent allowances in every mode. `network.blocked_cidrs` constrains the public-web rule; it is not an overriding deny on explicitly allowed engine peers. Other NetworkPolicies can widen permissions, and enforcement depends on the CNI, NAT and node-local exceptions. Existing TCP connections may outlive a policy change; verify revocation with new connections. Policy updates also need a running supervisor and successful writes to its namespace: an offline laptop or unavailable API cannot guarantee immediate removal of existing grants. Privileged engines and namespace-wide credentials are not a hostile-tenant boundary.

Use `reverse` for a native laptop service. Use selected Pod-to-Pod rules for direct engine access. See [configuration](configuration.md#network-settings) for limits, the [acceptance plan](pod-network-acceptance.md) for packet checks, and [known limits](known-limits.md) before enabling open access.
