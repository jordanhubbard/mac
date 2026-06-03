---
id: gketun-02
status: closed
resolved_by: "#59"
deps: []
links: [gketun-01, gketun-03]
created: 2026-06-03T00:00:00Z
type: bug
priority: 1
audit: jordanh-GKE-supervisord-validation
discovered_via: fresh-fleet-deploy
---
# network=none spokes get cluster-DNS shared-service URLs, not the tunnel ports

## Symptom

On a `network: none` spoke, the agent startup self-test reports Qdrant and
Firecrawl unreachable (`URLError: timed out`) even though the hub's reverse
tunnel is up and forwards those services:

```
checks: {qdrant_shared_memory: False, firecrawl_web_search: False, hermes_chat: True, ...}
blocking: ['Qdrant ... unreachable: timed out', 'Firecrawl ... unreachable: timed out']
```

## Root cause

The spoke's `MAC_QDRANT_URL` / firecrawl URL are derived from the **hub_url**
(e.g. `http://jordanh-hub.ov-agent-farm.svc.cluster.local:6333` and `:3002`).
In a cluster-pod environment those ports are blocked cross-pod (only port 22 is
open between pods — which is exactly why the SSH reverse tunnel exists). The
reverse tunnel forwards the hub's services to the spoke's localhost
(`-R 127.0.0.1:16333:127.0.0.1:6333`, `-R 127.0.0.1:13002:127.0.0.1:3002`), so
the spoke should reach Qdrant/Firecrawl at `127.0.0.1:16333` / `127.0.0.1:13002`,
not the hub FQDN. Confirmed: from the spoke, `curl 127.0.0.1:16333` → 200 and
`127.0.0.1:13002` → 200, while the FQDN ports time out.

The control-plane/qdrant/firecrawl forward ports (18789/16333/13002) are already
the tunnel convention (see `install_reverse_tunnel_on_hub`), and `MAC_HUB_URL`
is correctly set to `http://127.0.0.1:18789` for spokes — but the shared-service
URLs were not given the same tunnel-localhost treatment under `network=none`.

## Fix

Under `network: none` (reverse-tunnel topology), set the spoke's Qdrant and
Firecrawl URLs to the tunnel-forwarded localhost ports
(`127.0.0.1:16333` / `127.0.0.1:13002`) rather than the hub FQDN — mirroring how
`MAC_HUB_URL` already uses `127.0.0.1:18789`.

## Resolution

Resolved by #59 (network=none spokes use the tunnel-forwarded `127.0.0.1:16333`/`:13002` instead of the hub FQDN). Validated: the worker agent self-test's qdrant/firecrawl checks pass through the tunnel.
