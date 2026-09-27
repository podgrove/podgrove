# Test lanes

Install the `test` and `web-test` extras, a Docker Compose CLI, and a Playwright browser first (see [CONTRIBUTING](../CONTRIBUTING.md)). The normal suite uses isolated local processes and browser fixtures, without Kubernetes or a Docker daemon. Run it with:

```sh
python3 -m pytest -q -m 'not integration and not cluster'
```

The integration test is opt-in because it starts two disposable privileged Docker-in-Docker containers on the **local** Docker engine. It refuses a non-Unix-socket Docker host, requires 2 GiB of host disk headroom (4 GiB with Mongo), labels every outer test resource, uses unique names, caps each engine at 2 CPUs/1536 MiB, and removes its own containers and volumes after success or failure. It does not invoke `kubectl` or touch any cluster.

```sh
python3 scripts/e2e.py --mongo --output artifacts/docker-e2e
# The pytest entry point includes the Mongo isolation lane too:
PODGROVE_RUN_DOCKER_E2E=1 python3 -m pytest tests/integration -q
```

Read `results.json` for each observed assertion and timing; `commands.log` retains command results. Failed runs also collect engine logs and container listings. A report saying `passed` describes this local remote-engine lane, not Kubernetes scheduling, Kubernetes NetworkPolicy, or your application's business test suite.

| Fixture or scenario | What is exercised |
| --- | --- |
| `fixtures/parity/compose.yml` | HTTP service, WebSocket echo, directory and file binds, read-only binds, local-file config/secret, anonymous volume, named volumes, healthy Redis dependency, completed init job, service-name and alias DNS |
| `fixtures/parity/compose.overlay.yml` | Overlay order and an additional file mount; env-file values and optional missing env-file |
| `fixtures/parity/compose.override.yml` | Actual Compose `!override` parsing and replacement of published ports |
| `fixtures/parity/podgrove.yml` | Versioned config, optional profile, small size and TTL parsing |
| `fixtures/parity` edits | Atomic replacement of a single-file bind, new file, deleted nested file, directory content change, one-line Python change served under five seconds without an image change |
| Two separate engines | Same Compose names, isolated bind content, HTTP counters, Redis keys, and optional Mongo documents |
| Stack and Docker daemon restart | Named-volume and mirrored-file persistence |
| `fixtures/watch` | No `podgrove.yml`; `-f` alone, build-context upload, cached build, real remote `docker compose watch`, unchanged image after sync |
| `fixtures/invalid` | Unknown Podgrove key and unsupported Compose host-network key rejected before a test engine is created |

The Python edit fixture executes its tiny `live.py` module per request so the test measures transport latency without depending on an external framework. The watch fixture uses Compose's watcher over the remote Docker API. Neither measurement claims Vite or uvicorn itself was tested. The WebSocket fixture performs and validates an actual RFC 6455 handshake and masked echo frame, but the transport here is local Docker port publishing rather than Kubernetes port-forward.

All fixture secret values are public, inert test strings. Never substitute production credentials into these fixtures.


## Live exec export acceptance

Use an already running disposable environment in an explicitly approved namespace, an installed release executable, and a service containing Python. This probe does not start, refresh, delete or reap environments:

```sh
python3 scripts/check_exec_stream.py \
  --binary "$PODGROVE_BIN" \
  --project-directory /path/to/disposable-worktree \
  --context your-development-context --namespace your-development-namespace \
  --identity 012345abcdef --service probe --python python3 \
  --output /path/to/new-private-exec-evidence
```

Replace the identity with the exact value from that environment's `status --json`. The probe streams and hashes 29,284-byte, 8 MiB and repeated 64 MiB binary exports, verifies stdin EOF and stderr, and checks a deliberate remote exit 7. It also requires the same ready session after each command. An unexpected result stops the matrix without replaying a command. Only hashes/counts and safe status metadata are retained; temporary binary output is removed. A successful exit alone is insufficient: byte count and SHA-256 must both match.


## Forwarding soak of an installed release

The soak runner observes one existing disposable fixture; it never runs `up`, `down` or `reap`. The fixture must serve JSON containing `{"ok": true, "marker": "<marker-file contents>"}` at its recorded loopback endpoint. Select the exact release wheel and versioned executable, not an editable checkout:

```sh
python3 scripts/soak_forwarding.py \
  --binary "$PODGROVE_BIN" --wheel /path/to/podgrove-VERSION-py3-none-any.whl \
  --project-directory /path/to/disposable-worktree \
  --state-home /path/to/private-fixture-state \
  --context your-development-context --namespace your-development-namespace \
  --kubeconfig /path/to/private-client-kubeconfig --identity 012345abcdef \
  --url http://127.0.0.1:43123/ --marker-file /path/to/disposable-worktree/marker.txt \
  --output /path/to/new-private-soak-evidence --inject-after 300
```

All paths and the identity/endpoint must match that fixture. The default duration is 14,460 seconds (four hours plus one minute). Every observation verifies the selected installed payload against the receipt-bound wheel and preserves the exact resource UIDs. HTTP probes run every ten seconds and status observations every minute; status extends the fixture's TTL. The optional fault terminates one proven supervisor-owned `kubectl port-forward` child and requires recovery on the same endpoint. No node or cluster-scoped API is used. A short `--duration` is a smoke test and cannot produce `four_hour_proof: true`; interrupted runs, unexpected failures, changed identities/runtime bytes, and excessive sampling gaps fail the proof. The private JSON/JSONL evidence distinguishes injected recovery from unplanned errors.
