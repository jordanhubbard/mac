# Production Deployment

Three supported topologies:

1. **Single host, systemd** — one machine, one SQLite database, one FastAPI
   process. Suitable for dev fleets, personal Hermes runtimes, and pilot
   deployments. See `deploy/systemd/`.
2. **Containerized, single-instance** — image at `Dockerfile`. Same SQLite
   topology, but lifecycle is managed by the container runtime (Docker,
   Podman, k8s as a single-replica deployment). See the container section
   below.
3. **Kubernetes, multi-replica, CNPG-backed** — stateless `mac-api`
   Deployment in front of a CloudNativePG Postgres 17 cluster. Multiple
   `mac-api` replicas share the same durable state via `MAC_DATABASE_URL`.
   This is the K8s-native target topology — see
   [`deploy/k8s/README.md`](../deploy/k8s/README.md) and
   [`docs/k8s-native-rewrite-plan.md`](k8s-native-rewrite-plan.md).

`mac` is not designed for horizontal scale-out on SQLite. SQLite WAL handles
concurrent reads well and serializes writes through filesystem locks — so
`uvicorn --workers > 1` against the same SQLite DB *works*, but every write
call contends on the same lock. For multi-host, multi-replica, or
write-heavy fleets, use topology (3) (Postgres via CNPG) instead.

The backend selection is runtime: setting `MAC_DATABASE_URL` to a
`postgresql://...` DSN switches `mac-api` to `PostgresStore` without any
code change; leaving it unset keeps the legacy `SQLiteStore` path. Both
backends ship in the same wheel/image; pick at deploy time.

## Required configuration

| Variable | Required | Purpose |
|---|---|---|
| `MAC_SECRET_KEY` | yes | 32+ char secret; HKDF input for the Fernet key that encrypts secret values. Refuses to start without it. |
| `MAC_DATABASE_URL` | no | Postgres DSN (`postgresql://...` or `postgres://...`). When set, `mac-api` uses `PostgresStore` and ignores `MAC_DB`. The Postgres schema is auto-applied on startup (idempotent). |
| `MAC_PG_POOL_SIZE` | no | `psycopg_pool` max connections per `mac-api` replica. Default `10`. |
| `MAC_DB` | no | SQLite file path used when `MAC_DATABASE_URL` is unset. Default `./mac.db`. |
| `MAC_API_TOKEN` | no | Single admin bearer token. Set empty string is rejected. |
| `MAC_API_TOKENS` | no | JSON `{token: [scopes,...]}` for scoped auth. Mutually exclusive with `MAC_API_TOKEN`. |
| `HERMES_HOME` | no | Hermes state directory checked at startup. Default `~/.hermes`. |
| `ACC_DIR` | no | Legacy ACC data directory checked for migration/state references. Default `~/.acc`. |
| `MAC_HERMES_AGENT_DIR` | no | Hermes checkout inspected for the `slack_accounts.json` activation shim. Falls back to `HERMES_AGENT_DIR`, then `~/Src/hermes-agent` if present. |
| `MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM` | no | Set `0` to disable startup patching of an explicit `MAC_HERMES_AGENT_DIR`. Default enabled only when the checkout path is explicit. |
| `MAC_HERMES_APPLY_GATEWAY_RUNTIME_SHIM` | no | Set `0` to disable startup patching of Hermes gateway model/runtime overrides. Default enabled for explicit checkout paths. |
| `MAC_HERMES_GATEWAY_MODEL` | no | Per-agent model used by Hermes gateway conversations and mirrored to `HERMES_INFERENCE_MODEL` for oneshot worker execution. |
| `MAC_HERMES_GATEWAY_PROVIDER` | no | Runtime provider for the per-agent model. Fleet deploy uses `custom` so TokenHub remains the shared OpenAI-compatible endpoint. |
| `MAC_HERMES_GATEWAY_BASE_URL` | no | Optional explicit OpenAI-compatible base URL. Usually omitted because deployed hosts derive TokenHub's `/v1` endpoint from `TOKENHUB_URL`. |
| `MAC_HERMES_STARTUP_CHECK` | no | Set `0` to disable Hermes state and Slack startup checks. Enabled by default. |
| `MAC_REQUIRE_HERMES_STARTUP_READY` | no | Set `1` to fail `mac` startup when Hermes soul/memory/state references or Slack activation are not ready. |
| `MAC_HERMES_SLACK_HOME_CHANNEL_NAME` | no | Slack home-channel name, without `#`, used to write `~/.hermes/slack_home_channels.json` from `slack_accounts.json`. Empty skips discovery. |
| `MAC_HERMES_SYNC_SLACK_HOME_CHANNELS` | no | Set `0` to preserve existing Slack home-channel files without discovery. Default enabled. |
| `MAC_URL` / `MAC_HUB_URL` | no | MAC API endpoint used by Hermes-side `mac-hermes` operations. Fleet deploy points this at the hub. |
| `MAC_HERMES_INSTANCE_ID` | no | Hermes instance id for this runtime. Fleet deploy uses a deterministic `hermes_<agent>` id and registers it in MAC. |
| `MAC_WORKER_HERMES_INSTANCE_ID` | no | Worker agent binding to the Hermes instance id. This keeps MAC agent rows linked to their Hermes soul/runtime. |
| `MAC_AGENT_ID` | no | Deterministic MAC agent id for this runtime. Fleet deploy uses `agent_<agent>`. |
| `MAC_HERMES_RUNTIME_CONTEXT_FILE` | no | Hermes-visible task/project runtime contract JSON. Default `~/.hermes/mac-runtime-context.json`. |
| `MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN` | no | Human/agent-readable runtime contract summary. Default `~/.hermes/mac-runtime-context.md`. |
| `MAC_HERMES_RUNTIME_CONTEXT_REQUIRED` | no | Set `1` to make startup readiness fail if the MAC task/project runtime contract is missing, invalid, or not injected into the Hermes prompt builder. Fleet deploy enables this. |
| `MAC_HERMES_WORKSPACE` | no | Source workspace Hermes should treat as equivalent to an operator/Codex shell in the MAC repo. Fleet deploy sets this to `$MAC_HOME/src/mac`. |
| `MAC_PROJECT_CONTRACT_FILE` | no | Repository contract file for the Hermes direct-session capability bridge. Fleet deploy sets this to `$MAC_HERMES_WORKSPACE/.mac/project.yaml`. |
| `MAC_WORKER_EXECUTOR` | no | Executor command used by loop-mode workers. The default `~/.mac/bin/mac-hermes-task-executor` is part of the Hermes direct-session capability proof. |
| `MAC_SUPERVISOR_KIND` | no | Runtime supervisor selected by fleet deploy: `systemd`, `launchd`, or `supervisord`. |
| `MAC_MEMORY_TOPOLOGY_FILE` | no | Hermes-visible memory topology JSON. Default `~/.hermes/mac-memory-topology.json`. |
| `MAC_SHARED_SERVICES_MANAGER_AGENT` | no | Agent that owns hub-managed shared services. Defaults to the configured fleet hub. |
| `QDRANT_URL` / `QDRANT_ADDRESS` / `QDRANT_FLEET_URL` | no | Shared Qdrant level-2 memory endpoint. When set, Hermes startup readiness validates `/collections`. |
| `MAC_REQUIRE_QDRANT_MEMORY` | no | Set `1` to require shared Qdrant memory readiness. Fleet deploy enables this by default. |
| `MAC_QDRANT_MEMORY_ALLOW_DEGRADED` | no | Temporary operator override that allows startup when required Qdrant is missing or unreachable. |

Generate a secret key once:

```bash
openssl rand -base64 48
```

Store it in a secrets manager. Rotating the key requires re-encrypting all
secret values; today this is a manual procedure (re-emit every secret with
the new key).

## Systemd

```bash
# 1. Create the service user and data directory.
sudo groupadd --system mac
sudo useradd --system --gid mac --home-dir /var/lib/mac \
    --shell /usr/sbin/nologin mac
sudo install -d -o mac -g mac -m 0750 /var/lib/mac

# 2. Install mac globally (or into a venv at /usr/local/lib/mac).
sudo pip install /path/to/mac-0.1.0-py3-none-any.whl

# 3. Write the env file (mode 0600, owner root:mac).
sudo install -d -o root -g mac -m 0750 /etc/mac
sudo install -o root -g mac -m 0640 deploy/systemd/mac.env.example /etc/mac/mac.env
sudo $EDITOR /etc/mac/mac.env       # set MAC_SECRET_KEY, optionally MAC_API_TOKEN

# 4. Install and start the unit.
sudo install -o root -g root -m 0644 deploy/systemd/mac.service \
    /etc/systemd/system/mac.service
sudo systemctl daemon-reload
sudo systemctl enable --now mac.service

# 5. Verify.
sudo systemctl status mac.service
curl -fsS http://127.0.0.1:8000/health
```

The unit binds to `127.0.0.1:8000`. Put a TLS-terminating reverse proxy
(nginx, Caddy) in front for external access — do not expose the bare port.

## Fleet Setup Wizard

First-time deployments should use the setup wizard instead of hand-editing
deployment YAML:

```bash
bash setup.sh
```

The wizard asks for the hub, agents, SSH targets, OS families, supervisors,
Slack home channel, per-agent Hermes model selectors, worker mode, canary
policy, Qdrant shared-memory endpoint, fleet network provider, and optional
hub token. It writes:

- `~/.mac/fleets.yaml`: home-scoped multi-fleet topology, keyed by hub node.
- `~/.mac/.env`: caller-machine deploy settings and local secrets, mode 0600.

To deploy after the wizard:

```bash
set -a; . ~/.mac/.env; set +a
bash deploy/deploy-mac-fleet.sh --hub <hub-node>
```

The checked-in `deploy/fleet/config.yaml` is a generic sample only. It is
marked `sample: true`, and `deploy/deploy-mac-fleet.sh` refuses to deploy from
it unless `MAC_DEPLOY_ALLOW_SAMPLE_CONFIG=1` is set explicitly for tests.

## Reaching the Hub Node

The hub control plane binds to `hub_url` as declared in `~/.mac/fleets.yaml`.
How you reach it from a client machine depends on the network topology.

### Direct access (same network or VPN)

Hub is directly routable — no tunnel needed:

```bash
# Confirm health
curl http://<hub-host>:8789/health

# Deploy
bash deploy/deploy-mac-fleet.sh --hub <hub-node>
```

### SSH port forward (K8s, bastion, or private subnets)

Hub lives behind a bastion or inside a K8s cluster. Add a `Host` entry in
`~/.ssh/config` with `ProxyJump` (or `ProxyCommand`), then forward the control
port:

```
# ~/.ssh/config
Host my-hub
    HostName my-hub.cluster.local
    User horde
    ProxyJump horde@bastion.example.com:2222
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ForwardAgent yes
```

```bash
# Forward hub control port to localhost and open the UI
ssh -L 8789:127.0.0.1:8789 my-hub
# then: curl http://localhost:8789/health
# or:   open http://localhost:8789/ui
```

Set `hub_url: http://127.0.0.1:8789` in the fleet registry when using this
pattern — the deploy script reaches the hub through the forwarded port.

### Tailscale mesh (`provider: tailscale`)

Hub and spokes join the same Tailscale network. The hub is reachable at its
Tailscale IP or MagicDNS name without any SSH tunnel:

```yaml
# ~/.mac/fleets.yaml
defaults:
  network:
    provider: tailscale
    tailscale:
      auth_key_env: MAC_DEPLOY_TAILSCALE_AUTH_KEY
```

```bash
# Hub is reachable at its Tailscale IP, e.g. 100.x.x.x:8789
curl http://100.x.x.x:8789/health
bash deploy/deploy-mac-fleet.sh --hub <hub-node>
```

`MAC_DEPLOY_TAILSCALE_AUTH_KEY` must be set in `~/.mac/.env` before deploy.

### Headscale (self-hosted control plane, `provider: headscale`)

Headscale manages the WireGuard mesh. The fleet registry must declare the login
server, DNS assumption, health URL, and pre-auth key source:

```yaml
# ~/.mac/fleets.yaml
defaults:
  network:
    provider: headscale
    headscale:
      manage: false          # true if mac should install/manage the headscale binary on the hub
      login_server: https://headscale.example.com
      health_url: https://headscale.example.com/health
      preauth_key_source: env
      preauth_key_env: MAC_DEPLOY_HEADSCALE_PREAUTHKEY
      dns: magicdns
      ip_prefix: "100.64.0.0/10"
```

```bash
# Hub reachable at its headscale-assigned IP or MagicDNS name
curl http://hub.headscale.example.com:8789/health
bash deploy/deploy-mac-fleet.sh --hub <hub-node>
```

`MAC_DEPLOY_HEADSCALE_PREAUTHKEY` must be set in `~/.mac/.env`. With
`headscale.manage: true` the deploy script installs and configures the
headscale server on the hub node itself.

## One-Time ACC Replacement Deploy

For a configured fleet, use the fleet deploy script:

```bash
bash deploy/deploy-mac-fleet.sh --hub <hub-node>
```

Fleet deploy reads `~/.mac/fleets.yaml` by default. Override
`MAC_DEPLOY_FLEETS_CONFIG` when a different registry path is required. The hub
node name selects the fleet. Host-local secret env files still own tokens and
provider credentials.

Fleet mesh networking is configured under `defaults.network` or per-agent
`network` overrides in `~/.mac/fleets.yaml`. `provider: tailscale` is the
default and uses `MAC_DEPLOY_TAILSCALE_AUTH_KEY` from `~/.mac/.env` when
automatic join is desired. `provider: headscale` is an explicit advanced mode:
the fleet registry must declare `headscale.login_server`,
`headscale.health_url`, `headscale.preauth_key_source`,
`headscale.preauth_key_env`, and the DNS assumption. Managed-hub Headscale is
available with `headscale.manage: true`, but it should be treated as a shared
service with backup, monitoring, and recovery expectations rather than an
implicit default.

Fleet deploy is supervisor-driven, not Linux-systemd-only. Set
`MAC_DEPLOY_SUPERVISOR=auto` unless a host needs an explicit override. Auto
selects `launchd` on macOS, `systemd` on systemd Linux, and `supervisord` when
that is the available process supervisor. The selected value is written to
`MAC_SUPERVISOR_KIND` and recorded in deploy manifests.

Fleet deploy mirrors each configured per-agent model into `ACC_HERMES_GATEWAY_MODEL`,
`HERMES_INFERENCE_MODEL`, and `ACC_LLM_MODEL` so upstream Hermes gateway turns
and `mac-hermes-task-executor` oneshot work use the same per-agent identity.
Credentials remain in TokenHub or inherited host-local env files; mac only
writes model/provider selectors.

It ships this repository to each host, installs `mac` into `~/.mac/venv`,
redeploys upstream `NousResearch/hermes-agent` into `~/.mac/hermes-agent`,
applies the minimal multi-Slack Hermes patch, preinstalls configured Hermes
messaging dependencies before service start, applies the Hermes gateway
model/runtime shim, runs the ACC SQLite migration
dry-run and import from `~/.acc/data/fleet.db` or `~/.acc/data/acc.db`, and
starts a local `mac` service. Linux hosts get `mac.service`; macOS hosts get
`com.mac.control-plane`. The same deployment also starts a mac-managed Hermes
gateway from the upstream checkout: `mac-hermes-gateway.service` on Linux and
`com.mac.hermes-gateway` on macOS. It also installs a persistent `mac-agent`
registration service: `mac-agent.service` on Linux and `com.mac.agent` on
macOS.

When the local Git remote is available, fleet deploy installs `~/.mac/src/mac`
as a branch-tracking Git worktree and sets `MAC_SELF_UPDATE_REPO` to that path.
That lets the AgentBus repo-update control message pull future changes and
restart the listening `mac-agent` process without another manual deploy pass.

The fleet topology is hub-and-spoke, matching ACC. The configured hub exposes
the shared control plane URL from `hub_url`; spokes keep a host-local control
plane for local state and Hermes startup checks, but their `mac-agent` service
registers and heartbeats against the configured hub. By default the hub binds
`0.0.0.0` and spokes bind `127.0.0.1`.
Runtime lazy dependency installs are disabled after the preinstall step, and
`HERMES_REDACT_SECRETS=false` in inherited Hermes env files is corrected to
`true` because disabled redaction is treated as state drift.

The hub also owns the shared-services layer. Fleet deploy installs Qdrant on
the shared-services manager agent by default and configures every agent with
the same `QDRANT_URL` / `QDRANT_FLEET_URL`. Each agent receives a
Hermes-visible `~/.hermes/mac-memory-topology.json` plus `.env` pointers that
describe local Hermes soul/conversation state, mac operational provenance, and
hub-managed shared level-2 memory. `/startup/hermes` reports
`qdrant_level2` readiness using redacted endpoints only.

Deployment logs and migration reports are written under `~/.mac/logs/` on each
host:

- `deploy-*.log`
- `deploy-manifest-*-pre.json`, `deploy-manifest-*-post.json`, and
  `deploy-manifest-latest.json`
- `rollback-*.sh` and `rollback-latest.sh`
- `acc-migration-dry-run.json`
- `acc-migration-import.json`
- `acc-migration-status.json`
- `startup-hermes.json`
- `hermes-messaging-deps.json`
- `hermes-home-channel-sync.json`
- `hermes-redaction-normalization.json`
- `hermes-log-summary.json`
- `mac-service-journal.txt` on Linux, or `mac-service.log` on macOS
- `hermes-gateway-journal.txt` on Linux, or `hermes-gateway.log` on macOS
- `mac-agent-journal.txt` on Linux, or `mac-agent.log` on macOS
- `hub-agents.json`

The activation shim for `slack_accounts.json` is intentionally applied by
`mac` startup, not by the deploy script, so this path exercises the startup
patch capability.

To roll back the most recent deployment on a host:

```bash
~/.mac/logs/rollback-latest.sh
```

The rollback script restores the prior mac source tree, mac venv, Hermes
checkout, and service definitions or launchd plists that existed before the
deploy pass, then restarts the mac-managed services.

## Worker Agents

The control-plane service does not execute tasks by itself. Each execution host
must run a worker process that registers or refreshes its machine/agent row,
heartbeats, then claims eligible open work with a real executor. Fleet deploy
installs that process as a service in `heartbeat` mode by default so hosts are
visible in the configured hub registry without claiming imported ACC work
prematurely:

```bash
mac-agent --url http://hub.example.internal:8789 --register \
  --agent-name worker-1 --hostname worker-1.local \
  --capabilities python,ops,review --resources '{"capacity":2}' \
  --heartbeat-only

mac-agent --url http://hub.example.internal:8789 --register \
  --agent-name worker-1 --capabilities python,ops,review \
  --workspace ~/.mac-agent/workspaces --loop \
  --executor ~/.mac/bin/mac-hermes-task-executor
```

Use `--heartbeat-only` during deploy validation when you want fleet visibility
without claiming migrated ACC work. Start the `--loop` form only after the
executor command is the intended production worker. Successful executions write
log evidence, move tasks to `needs_review`, and ask the control plane to run
the default review workflow. The default workflow assigns a healthy agent that
has never owned the task as reviewer, then waits for a separate signed
`review_verdict` evidence row from that reviewer. It publishes/completes the
task only when both executor evidence and reviewer verdict are verifiable.
Failed executions fail the task with evidence attached.

Every mac-managed subprocess records a short-retention command audit event on
the hub. The log captures `command_id`, agent, task, sanitized `argv`, `cwd`,
start/end timestamps, return code, output byte counts, and output hashes. The
default retention is 24 hours (`MAC_COMMAND_AUDIT_RETENTION_SECONDS`), and the
latest rows are visible from `/command-audit` and the dashboard Observability
view. This is operational telemetry for proving agents are doing work; a future
security audit store can consume the same event shape externally.

The command audit is not a durable compliance archive. Its current job is to
make the last day of worker behavior visible without relying on local shell
history or unbounded per-host logs.

Executor success is not completion. A zero return code only means the executor
reported without crashing. For the default workflow to auto-approve and publish,
the evidence metadata must include a `mac.worker_evidence.v1` verification
manifest. Repo/code work must include a pushed git artifact (`head_sha`,
`remote_ref` or PR URL, `pushed=true`, `dirty=false`, changed files) plus at
least one passing test/check. Documentation or investigation work must include a
pushed repo artifact or explicit artifacts plus passing checks. Deployment work
must include deployment targets/services and passing checks. Thin reports,
local-only diffs, missing manifests, failing checks, or unverifiable claims stay
in `needs_review`/`reviewing` for manual handling.

During execution, the worker renews its active task lease. A successful renewal
also refreshes the owning agent's `last_seen_at`, keeps it `busy`, and preserves
`current_task_id`, so long-running work remains visible as live without allowing
an invalid `idle` heartbeat while the lease is active.

For already-migrated or pre-upgrade rows that are stuck in `needs_review`, run
the backlog tick against the hub:

```bash
curl -X POST 'http://hub.example.internal:8789/reviews/default/tick?limit=100&actor=operator'
```

Before enabling executor-backed claiming, use `dry-run` mode to record routing
candidates without creating leases:

```bash
MAC_DEPLOY_WORKER_MODE=dry-run
MAC_DEPLOY_WORKER_REQUIRE_CANARY=1
MAC_DEPLOY_WORKER_ALLOWED_PROJECTS=mac-canary
```

Dry-run mode emits `worker.routing.policy`,
`worker.routing.dry_run_candidate`, or `worker.routing.no_candidate` events
into `/observability` and `/observability/stream`. It must show only synthetic
canary work before loop mode is enabled.

To enable executor-backed claiming from deploy config, set:

```bash
MAC_DEPLOY_WORKER_MODE=loop
MAC_DEPLOY_WORKER_CAPABILITIES=ops,python,hermes,review
MAC_DEPLOY_WORKER_REQUIRE_CANARY=1
MAC_DEPLOY_WORKER_ALLOWED_PROJECTS=mac-canary
```

The generated service then runs `mac-agent --register --loop` against
`MAC_HUB_URL`, using `MAC_WORKER_TOKEN` from `~/.mac/mac.env`. The default
executor wrapper is `~/.mac/bin/mac-hermes-task-executor`, which calls the
deployed upstream Hermes checkout in one-shot mode. The runtime proof treats
that executable as a required session capability, so a deployed agent is not
considered ready for Codex-like task work unless the executor path is present
and executable.

Fleet deploy deliberately avoids printing the mac-agent process command line.
On Linux it reports `mac-agent.service` with `systemctl show` summary fields
instead of `systemctl status`, because the service wrapper currently passes the
worker token to `mac-agent` as process argv. Deployment logs should therefore
show service state, PID, and restart count, but not the bearer token. Operators
should continue to treat host-level process inspection as privileged access.

Workers advertise `review` by default so the default review workflow can pick
real second-eye reviewers. During registration the worker persists its
attestation key into `~/.mac/mac.env`; if an older deploy missed that one-time
key, the service rotates a replacement before it signs new evidence. Rotation
is explicit recovery behavior and invalidates old signatures from that agent.

Loop mode is canary-gated by default. To make a worker eligible for real
migrated work, explicitly set `MAC_DEPLOY_WORKER_REQUIRE_CANARY=0` and narrow
the blast radius with project or metadata filters first.

## Beads Bridge

The hub can turn ready Beads into durable mac tasks automatically. Register
each repository once:

```bash
mac --db ~/.mac/mac.db bridge beads register mac ~/.mac/src/mac \
  --source repo-beads-mac --project repo-beads-mac
```

Every registered repository must include a repository runtime contract at
`.mac/project.yaml`. The bridge validates that contract at registration and on
each poll, and rejects repositories that do not declare their supported host
families, bootstrap command, canonical test command, and required evidence.
See [Repository Runtime Contract](repository-runtime-contract.md).

The deploy script enables heartbeat polling on the configured hub agent and
registers the deployed mac checkout by default through:

```bash
MAC_BEADS_BRIDGE_ON_HEARTBEAT=1
MAC_BEADS_BRIDGE_HUB_AGENT=<hub-agent-name>
MAC_BEADS_AUTO_PULL=1
MAC_BEADS_CLI=$HOME/.mac/bin/bd
MAC_BEADS_BRIDGE_ROOT=$HOME/.mac/beads-checkouts
MAC_BEADS_REPOSITORIES=mac=$HOME/.mac/src/mac:repo-beads-mac:repo-beads-mac::30
```

The deploy bootstrap installs the `gh` CLI into `~/.mac/bin/gh` and the `bd`
CLI into `~/.mac/bin/bd`. `gh` is required so worker branches can become GitHub
PRs instead of stranded pushed refs. `bd` is built from the configured Beads
source (`MAC_DEPLOY_BEADS_REPO_URL`, `MAC_DEPLOY_BEADS_REF`) when it is not
already present, then deploy runs `bd bootstrap --yes` for each configured
Beads repository and `bd dolt pull` when possible so fresh clones and bridge
checkouts have a writable, current Beads database, not only tracked JSONL.
Production deploys set
`MAC_BEADS_RESTORE_TRACKED_EXPORTS=1`, so Beads may keep its embedded
operational state while tracked export noise in `.beads/issues.jsonl` and
`.beads/config.yaml` is restored after bootstrap and claim/close sync.

The hub does not poll the live runtime checkout directly. Each git-backed
registered repository is cloned or refreshed into a managed bridge checkout
under `MAC_BEADS_BRIDGE_ROOT`, and polling plus Beads claim/close sync run
there. The registered path remains the canonical project path that workers use
to create task-owned worktrees, but bridge polling is isolated from local
self-update, deploy, and repair activity in `~/.mac/src/mac`.

On each hub heartbeat or lease renewal, the control plane
polls every enabled registered repository whose poll interval has elapsed. The
poller first refreshes the managed bridge checkout. With `MAC_BEADS_AUTO_PULL=1`
(default), the bridge checkout is cloned if missing, fetches its upstream, and
is checked out to the registered branch's upstream ref before Beads are read.
Dirty tracked files in the managed bridge checkout are reset because that tree
is owned by the bridge; dirty files in the registered runtime checkout are
recorded in `bridge.beads.repository_source` but do not block polling. If the
managed checkout cannot be cloned or refreshed, the bridge does not silently
poll ambiguous local state; it returns `source_refresh_error`, logs
`bridge.beads.repository_source`, writes a dashboard/Hermes notification, and
creates one idempotent remediation task for the agent that owns that registered
environment. Ownership is resolved from repository metadata
(`owner_agent_id`/`owner_agent_name` and aliases), then from the polling hub
agent. The poller then runs `bd ready --json` when available, falling back to
`.beads/issues.jsonl` parsing for simple local fixtures. Only `open` Beads with
no active blockers are imported; blocked
Beads wait until their blockers close. Imports are idempotent through the
`project_items(source, external_id)` unique key.

When `bd ready --json` succeeds, the Beads database is treated as canonical.
The tracked `.beads/issues.jsonl` export is still parsed as a derived copy and
compared against the canonical ready IDs. If ready issues exist only in JSONL,
mac opens an `integration_findings` row of type
`beads.export_drift.jsonl_only_ready`, emits an operator notification, and does
not import those export-only issues until the Beads DB exposes them. This keeps
the bridge from silently choosing the wrong authority during DB/export drift.
If the managed bridge checkout's embedded Dolt database cannot pull from the
configured remote, mac moves that disposable DB aside, re-runs
`bd bootstrap --yes`, and retries `bd dolt pull` before declaring drift.
See [Integration Authority Contract](integration-authority-contract.md).

The hub also advances the default review/publication workflow from heartbeat
when `MAC_REVIEW_TICK_ON_HEARTBEAT=1` and `MAC_REVIEW_TICK_HUB_AGENT` is set
to the configured hub agent. Fleet deploy sets it from the same configured hub
agent used by `MAC_BEADS_BRIDGE_HUB_AGENT` unless explicitly overridden.
The tick only moves tasks when required evidence, reviewer verdicts, and
publication targets are present; otherwise it records explicit waiting reasons
in observability.

Useful operator commands:

```bash
mac --db ~/.mac/mac.db bridge beads repos
mac --db ~/.mac/mac.db bridge beads poll --force
mac --db ~/.mac/mac.db integrations findings --source-kind beads_repository --status open
```

Imported tasks keep Beads provenance in `task.metadata.origin` and
`task.metadata.acc_metadata`, use the repository source as their mac project,
and are immediately eligible for normal worker claiming.

### Beads Human Ledger

mac's internal task history remains the authoritative execution ledger, but
humans usually read the Bead first. For Beads-backed work, mac mirrors key
workflow milestones into Beads comments with the prefix `mac-ledger v1`.

Mirrored milestones include:

- `imported`: a ready Bead became a durable mac task.
- `claimed`: an agent claimed the task and an attempt started.
- `state_running`, `state_needs_review`, `state_reviewing`,
  `state_failed`, `state_cancelled`, `state_open`: major task gates.
- `evidence_added`: executor, review, artifact, test, publication, or log
  evidence was recorded.
- `review_requested`, `review_completed`, `review_retracted`: review gates and
  reviewer changes.
- `published`: publication target and publication id.
- `retry_reopened`, `retry_exhausted`: Beads reconciliation decisions for
  failed mapped tasks.

For failed work, mac also appends a concise failure summary to the Bead notes
and comments. The summary is derived from mac task history and evidence, and
includes the failure reason, verification problems, evidence id, and retry
exhaustion state when available. This is the human-facing breadcrumb for why an
otherwise open Bead is not currently progressing.

Lease renewals are intentionally not mirrored to Beads; they remain in mac task
history and observability so issue logs do not fill with heartbeat noise.
Ledger comment failures are logged as `bridge.beads.ledger_failed` and do not
roll back the primary mac task transition.

## AgentBus

`/messages` remains the constrained structured control bus and still rejects
execution-shaped payloads. Agent-to-agent content exchange uses `/agentbus`
instead:

- `POST /agentbus/streams` opens a typed stream with `content_type`, `topic`,
  optional task linkage, and optional recipient.
- `POST /agentbus/streams/{id}/chunks` appends ordered JSON, text, or base64
  chunks. `final=true` closes the stream atomically after the chunk.
- `GET /agentbus/streams/{id}/chunks` reads durable chunks after a sequence.
- `GET /agentbus/streams/{id}/events` tails chunks as NDJSON for streaming
  consumers.

This is a typed transport channel, not an execution channel. Agents must still
turn received content into explicit task/evidence/review actions through the
normal API.

mac-agent also listens for one constrained AgentBus control topic before it
claims tasks:

- Topic: `mac.repo.update.v1`
- Content type: `application/vnd.mac.repo-update+json`
- Payload schema: `mac.agentbus.repo_update.v1`

The listener runs `git pull --ff-only` in `MAC_SELF_UPDATE_REPO` and exits for
service-manager restart only when the worktree HEAD changes. Dirty worktrees,
non-git source trees, invalid remotes/branches, and pull failures are reported
as result streams instead of being forced. Result streams use topic
`mac.repo.update.result.v1` and content type
`application/vnd.mac.repo-update-result+json`.

To broadcast a source update from the hub:

```bash
mac --db ~/.mac/mac.db agentbus repo-update agent_<hub> --all-agents
```

## Roles, Workflows, and Provisioning

Production mac includes an API-level organization model for coordinated work:

- `/roles` stores the role catalog used to describe agent jobs, prompts,
  required/default capabilities, optional hardware requirements, and tenant
  scope. `/roles/seed` loads the built-in Loom-style role set.
- `/agents/{id}/role` assigns a role to a registered agent. If the agent is
  bound to a Hermes persona, role assignment respects that persona's allowlist.
- `/provisioning/requests` records missing capacity requests when the fleet has
  no suitable agent for a role/capability requirement.
- `/workflows` stores versioned DAG definitions. `/workflows/import-yaml` and
  `/workflows/seed` provide operator-friendly loading paths.
- `/workflows/{id}/start`, `/workflows/runs`, and `/workflows/runs/tick` run
  and sweep workflows. Each node creates a normal mac task, so dispatch,
  evidence, review, publication, command audit, and Beads ledger behavior stay
  the same as single-task work.

`/dashboard/state` includes workflow-run summary data for UI clients. Full
visual workflow authoring is still an operator-facing gap in the checked-in
dashboard; use the API/CLI for workflow creation and editing until that UI is
built.

## Docker / Podman

```bash
docker build -t mac:latest .

docker run -d --name mac \
    -e MAC_SECRET_KEY="$(openssl rand -base64 48)" \
    -e MAC_API_TOKEN="$(openssl rand -hex 32)" \
    -v mac-data:/var/lib/mac \
    -p 127.0.0.1:8000:8000 \
    --restart unless-stopped \
    mac:latest

# Healthcheck is built into the image; `docker ps` shows (healthy) once up.
curl -fsS http://127.0.0.1:8000/health
```

For Kubernetes, ship the same image as a single-replica `Deployment` with a
PVC mounted at `/var/lib/mac`. Use a `ConfigMap` for non-secret env and a
`Secret` for `MAC_SECRET_KEY` / `MAC_API_TOKEN`.

## Backups

`mac.db` is a SQLite WAL database. Snapshot with SQLite's online backup:

```bash
sqlite3 /var/lib/mac/mac.db ".backup '/backups/mac-$(date +%Y%m%dT%H%M%SZ).db'"
```

WAL means a plain `cp` is unsafe (you'll miss the WAL file or copy
inconsistent state). The `.backup` command coordinates with the running
process. Restore is a file copy while the service is stopped.

## Observability

- `GET /health` is the liveness/readiness signal. Returns `{"status":"ok"}`.
- `GET /startup/hermes` returns the redacted Hermes startup report: file
  existence/size/mtime metadata, warning strings, and Slack activation status.
- `GET /events` is the unified audit stream — point a log shipper (vector,
  promtail, fluent-bit) at it with `since=` advancing every poll, or scrape
  the SQLite tables directly.
- `mac --db /var/lib/mac/mac.db events list --since <iso>` is the operator's
  one-shot "what just happened" query.
- `POST /observability/metrics` and `POST /observability/logs` ingest
  layer/source/name/level observations from workers, Hermes adapters, deploy
  scripts, and external monitors. POST requires `agent` scope when API tokens
  are enabled.
- `GET /observability` lists persisted observations; `GET
  /observability/summary` returns dashboard aggregates and latest metric
  snapshots; `GET /observability/stream` tails observations as NDJSON for live
  dashboards or collectors.
- `GET /notifications` lists the durable operator notification outbox for task
  lifecycle, review, publication, and bridge-stale events. `POST
  /notifications/{id}/delivered` marks entries delivered, failed, or skipped.

The FastAPI middleware records per-request `http.request.duration_ms` metrics
and `http.request` logs. Control-plane task, agent, project, fleet, secret,
environment, rollout, and eval events are mirrored into the observability
stream with their original subject ids. The dashboard Observability tab uses
URL-addressable filters, the summary endpoint, and an NDJSON subscription to
visualize the live stream, unified events, command audit, and the operator
notification outbox.

## Upgrade procedure

1. Stop the service.
2. Snapshot `mac.db` (the `.backup` command above).
3. Install the new wheel / pull the new image.
4. Start the service. The schema migrator (`store._migrate`) is additive only
   — new columns get `_ensure_column`'d; old data survives.
5. Verify `GET /health` and a recent `GET /events` query.

If a migration fails, restore the snapshot and pin the prior version. The
project does not yet support downgrades through schema deletes.

## Kubernetes + CNPG (K8s-native topology)

For multi-replica `mac-api`, deploy the manifests under `deploy/k8s/`:

```bash
# 1. CloudNativePG cluster (3 instances of postgres:17, bootstraps db `mac`).
kubectl apply -k deploy/k8s/cnpg
kubectl -n mac wait --for=condition=Ready cluster/mac-pg --timeout=10m

# 2. mac-api Deployment + Service (replicas: 2, no PVC, reads MAC_DATABASE_URL
#    from the CNPG-managed `mac-pg-app` Secret).
kubectl apply -k deploy/k8s/mac-api
```

The full apply order, ExternalSecret wiring, ArgoCD `Application`, and
backup config are documented in
[`deploy/k8s/README.md`](../deploy/k8s/README.md). The persistence layer
is portable across SQLite and Postgres because every `mac-api` SQL
string is in SQLite dialect; the `PostgresStore` translates placeholders
and provides a `json_extract` SQL function shim so the ~50 service
modules need no per-backend branching. See
[`docs/k8s-native-rewrite-plan.md`](k8s-native-rewrite-plan.md) for the
Phase 3-5 roadmap.

## Known limitations

- SQLite topology is single-writer. Use the Kubernetes + CNPG topology
  for multi-replica deployments.
- No built-in TLS. Put a reverse proxy in front.
- `MAC_SECRET_KEY` rotation is manual.
- Live SQLite → Postgres data migration tool is not yet shipped (K8s
  Phase 3.7 — pending). Greenfield CNPG deploys start with an empty
  Postgres schema applied automatically; existing SQLite deployments
  upgrading to the K8s topology must currently re-create state.
