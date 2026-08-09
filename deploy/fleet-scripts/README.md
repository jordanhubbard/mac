# deploy/fleet-scripts

This directory documents the **fleet deployment scripts** in `deploy/` and
holds sample **per-node manifest fragments** generated after a successful
deploy run.

## Deployment scripts

### deploy-mac-fleet.sh

The main fleet deploy orchestrator. It reads a fleet spec
(`~/.mac/specs/<fleet-name>.fleet.yaml`), SSHs into each node in turn, and
runs the appropriate `install-*.sh` scripts to bring every node into its
target role. On completion it writes a per-node manifest to
`~/.mac/logs/deploy-manifest-<stage>-<ts>.json` capturing the git rev, model
config, and service options used for that deploy.

**Fleet topology** stays outside git in `~/.mac/specs/` and
`~/.mac/fleets.yaml`. The repo ships only generic, de-personalized samples
under `deploy/fleet/samples/`.

```bash
# Deploy to a named fleet spec (topology stays outside git).
deploy/deploy-mac-fleet.sh --fleet ~/.mac/specs/<fleet-name>.fleet.yaml

# Dry-run (no SSH, no writes).
deploy/deploy-mac-fleet.sh --fleet ~/.mac/specs/<fleet-name>.fleet.yaml --dry-run
```

### install-agent-journal-service.sh

Installs a systemd timer that runs `mac admin journal snapshot` once a day,
capturing the agent's soul and memory state (SOUL.md, USER.md, MEMORY.md,
memories/, mood, config) into `~/.mac/journal/<date>/`.

### install-firecrawl-gateway.sh

Installs and starts the hub web-search gateway — a lightweight
Firecrawl v2-compatible service. Worker agents point `FIRECRAWL_API_URL`
at the hub so the fleet shares one search gateway instead of each host
running its own.

### install-fleet-context-service.sh

Installs the fleet-context background service that aggregates operational
context (running tasks, agent status, recent logs) and makes it queryable
by agents and operators.

### install-headscale.sh

Installs the headscale control plane on the hub node. Used when the fleet
spec selects `network.provider: headscale` (self-hosted Tailscale control
plane) instead of Tailscale SaaS.

### install-nap-tick-service.sh

Installs the nap-tick service, which manages agent sleep/wake cycles to
reduce resource usage when no tasks are available.

### install-observability-prune.sh

Installs a periodic pruning job that keeps observability data (logs,
metrics, traces) under configurable size limits, preventing storage
exhaustion on long-running nodes.

### install-qdrant-service.sh

Installs Qdrant (the vector store used by agent memory and embedding
search). Configures the service with memory limits and port bindings
from the fleet spec.

### install-tailscale.sh

Installs Tailscale and joins the fleet mesh network. Supports two control
plane modes:

- **tailscale cloud** — Tailscale SaaS (requires `TAILSCALE_AUTH_KEY`).
- **headscale** — self-hosted control plane (requires `HEADSCALE_AUTH_KEY`
  and `HEADSCALE_CONTROL_URL`).

### install-webdav-server.sh

Installs a lightweight WebDAV server that agents and operators use for
artifact exchange (build outputs, reports, task evidence) without
requiring object-storage credentials on every node.

## Fleet configuration conventions

Fleet **topology** belongs outside git:

| Location | Contents |
|---|---|
| `~/.mac/specs/<fleet-name>.fleet.yaml` | Per-fleet spec (agent names, hosts, model, keys) |
| `~/.mac/fleets.yaml` | Generated registry of known fleets |
| `~/.mac/.env` (mode 0600) | Secrets and API keys |

The repo ships only de-personalized CSP samples under
`deploy/fleet/samples/`. Copy one to `~/.mac/specs/` and fill in the
`<placeholders>` before deploying.

Generic role names used in examples: **hub**, **worker-1**, **worker-2**,
**gpu-worker**. Use placeholders `<user>`, `<host>`, `<fleet-name>`,
`<mesh-ip>` for values that differ per operator.

## manifest-fragments/

The `manifest-fragments/` subdirectory holds sample post-deploy manifest
fragments. Each `.json` file illustrates the structure written by
`deploy-mac-fleet.sh` to `~/.mac/logs/deploy-manifest-<stage>-<ts>.json`
on the origin host after a successful deploy run.

These samples use placeholder values only and must not contain real
operator identity, hostnames, or IP addresses.
