# Production Deployment

Three supported topologies:

1. **Single host, systemd** — one machine, one SQLite database, one FastAPI
   process. Suitable for dev fleets, personal Hermes runtimes, and pilot
   deployments. See `deploy/systemd/`.
2. **Containerized, single-instance** — image at `Dockerfile`. Same SQLite
   topology, but lifecycle is managed by Docker Engine/Moby or k8s as a
   single-replica deployment. See the container section below.
3. **Kubernetes, multi-replica, Postgres-backed** — stateless `mac-api`
   Deployment in front of an externally-managed Postgres 17 cluster.
   Multiple `mac-api` replicas share the same durable state via
   `MAC_DATABASE_URL`. The cluster itself (CloudNativePG, RDS, Cloud
   SQL, vendor-managed, etc.) is provisioned outside this repo and its
   DSN is supplied via the `mac-api-config` Secret. See
   [`deploy/k8s/README.md`](https://github.com/jordanhubbard/mac/blob/main/deploy/k8s/README.md) and
   [`docs/k8s-native-rewrite-plan.md`](archive/field-notes/k8s-native-rewrite-plan.md).

`mac` is not designed for horizontal scale-out on SQLite. SQLite WAL handles
concurrent reads well and serializes writes through filesystem locks — so
`uvicorn --workers > 1` against the same SQLite DB *works*, but every write
call contends on the same lock. For multi-host, multi-replica, or
write-heavy fleets, use topology (3) (Postgres) instead.

The backend selection is runtime: set `MAC_DATABASE_URL` to a
`postgresql://...` DSN for `PostgresStore`, or set `MAC_DB` to an explicit
SQLite path. There is no implicit home-directory database. Both backends ship
in the same wheel/image; pick one at deploy time.

Only a hub or stateless API replica is a control-plane server. Fleet spokes are
clients: their generated environment has `MAC_CONTROL_PLANE_ROLE=client`, no
`MAC_DB` or `MAC_DATABASE_URL`, and their worker/gateway processes use
`MAC_HUB_URL`. A redeployed spoke archives an inactive legacy `mac.db`; active
legacy tasks block deployment until explicitly migrated to the hub. Store
construction also rejects `MAC_CONTROL_PLANE_ROLE=client` even if a stale
database variable is reintroduced.

## Required configuration

| Variable | Required | Purpose |
|---|---|---|
| `MAC_SECRET_KEY` | yes | 32+ char secret; HKDF input for the Fernet key that encrypts secret values. Refuses to start without it. |
| `MAC_CONTROL_PLANE_ROLE` | yes for fleet deploys | `hub` for the single database-owning authority; `client` for database-free spokes. |
| `MAC_DATABASE_URL` | conditional | Postgres DSN (`postgresql://...` or `postgres://...`). Required unless `MAC_DB` is set. When set, `mac-api` uses `PostgresStore` and ignores `MAC_DB`. The Postgres schema is auto-applied on startup (idempotent). |
| `MAC_PG_POOL_SIZE` | no | `psycopg_pool` max connections per `mac-api` replica. Default `10`. |
| `MAC_DB` | conditional | Explicit SQLite control-plane path. Required unless `MAC_DATABASE_URL` is set. No client-side `~/.mac/mac.db` is created implicitly. |
| `MAC_API_TOKEN` | no | Single admin bearer token. Set empty string is rejected. |
| `MAC_API_TOKENS` | no | JSON `{token: [scopes,...]}` for scoped auth. Mutually exclusive with `MAC_API_TOKEN`. |
| `HERMES_HOME` | no | Hermes state directory checked at startup. Default `~/.hermes`. |
| `ACC_DIR` | no | Legacy ACC data directory checked for migration/state references. Default `~/.acc`. |
| `MAC_HERMES_AGENT_DIR` | no | Hermes checkout inspected for the `slack_accounts.json` activation shim. Falls back to `HERMES_AGENT_DIR`, then `~/Src/hermes-agent` if present. |
| `MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM` | no | Set `0` to disable startup patching of an explicit `MAC_HERMES_AGENT_DIR`. Default enabled only when the checkout path is explicit. |
| `MAC_HERMES_APPLY_GATEWAY_RUNTIME_SHIM` | no | Set `0` to disable startup patching of Hermes gateway model/runtime overrides. Default enabled for explicit checkout paths. |
| `MAC_HERMES_GATEWAY_MODEL` | no | Per-agent model used by Hermes gateway conversations and mirrored to `HERMES_INFERENCE_MODEL` for oneshot worker execution. |
| `MAC_HERMES_GATEWAY_PROVIDER` | no | Runtime provider for the per-agent model. Fleet deploy normally uses `custom` so Hermes sends OpenAI-compatible requests through MAC's router. |
| `MAC_HERMES_GATEWAY_BASE_URL` | no | OpenAI-compatible base URL for Hermes. Fleet deploy writes the hub's local in-mac `/v1` endpoint on the hub and the hub or wing-router `/v1` endpoint on spokes. |
| `MAC_HERMES_STARTUP_CHECK` | no | Set `0` to disable Hermes state and Slack startup checks. Enabled by default. |
| `MAC_REQUIRE_HERMES_STARTUP_READY` | no | Set `1` to fail `mac` startup when Hermes soul/memory/state references or Slack activation are not ready. |
| `MAC_HERMES_SLACK_HOME_CHANNEL_NAME` | no | Slack home-channel name, without `#`, used to write `~/.hermes/slack_home_channels.json` from `slack_accounts.json`. Empty skips discovery. |
| `MAC_HERMES_SYNC_SLACK_HOME_CHANNELS` | no | Set `0` to preserve existing Slack home-channel files without discovery. Default enabled. |
| `MAC_URL` / `MAC_HUB_URL` | no | MAC API endpoint used by Hermes-side `mac-hermes` operations. Fleet deploy points this at the hub. |
| `MAC_CLIENT_PRINCIPALS_FILE` | no | Hub-local hashed registry for scoped client enrollment. Fleet deploy sets `$MAC_HOME/client-principals.json`; permissions must be `0600`. The API hot-reloads issuance and revocation. |
| `MAC_HERMES_INSTANCE_ID` | no | Hermes instance id for this runtime. Fleet deploy uses a deterministic `hermes_<agent>` id and registers it in MAC. |
| `MAC_WORKER_HERMES_INSTANCE_ID` | no | Worker agent binding to the Hermes instance id. This keeps MAC agent rows linked to their Hermes soul/runtime. |
| `MAC_AGENT_ID` | no | Deterministic MAC agent id for this runtime. Fleet deploy uses `agent_<agent>`. |
| `MAC_HERMES_RUNTIME_CONTEXT_FILE` | no | Hermes-visible task/project runtime contract JSON. Default `~/.hermes/mac-runtime-context.json`. |
| `MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN` | no | Human/agent-readable runtime contract summary. Default `~/.hermes/mac-runtime-context.md`. |
| `MAC_HERMES_RUNTIME_CONTEXT_REQUIRED` | no | Set `1` to make startup readiness fail if the MAC task/project runtime contract is missing, invalid, or not injected into the Hermes prompt builder. Fleet deploy enables this. |
| `MAC_HERMES_WORKSPACE` | no | Source workspace Hermes should treat as equivalent to an operator/Codex shell in the MAC repo. Fleet deploy sets this to `$MAC_HOME/src/mac`. |
| `MAC_PROJECT_CONTRACT_FILE` | no | Repository contract file for the Hermes direct-session capability bridge. Fleet deploy sets this to `$MAC_HERMES_WORKSPACE/.mac/project.yaml`. |
| `MAC_WORKER_EXECUTOR` | no | Executor command used by loop-mode workers. The default `~/.mac/bin/mac-hermes-task-executor` is part of the Hermes direct-session capability proof. |
| `GH_TOKEN` / `GITHUB_TOKEN` | no | GitHub HTTPS credential used by task, review, publication, and pushed-ref verification commands. The credential may appear only in the individual Git command and must not persist in `origin`, evidence, logs, or memory. |
| `GITEA_TOKEN` | no | Gitea HTTPS credential for the same Git operations. `MAC_TASK_GIT_TOKEN` is the host-mode fallback when no host-specific token is set. |
| `MAC_DEPLOY_GH_TOKEN` | no | Fleet-deploy input copied into the managed runtime as `GH_TOKEN`. Keep it in the host-local `~/.mac/.env`, never in `fleets.yaml` or a committed spec. |
| `MAC_REPOSITORY_REF_RECONCILER_MODE` | no | Managed task-branch reconciler mode: `off`, `audit`, or `prune`. Runtime default `off`; fleet deployment defaults the hub to `prune` and spokes to `off`. |
| `MAC_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS` | no | Delay between automatic passes, bounded from `60` through `604800`. Hub default `86400` (daily). |
| `MAC_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS` | no | Delay before the first automatic pass, bounded from `0` through `86400`. Hub default `300`. |
| `MAC_REPOSITORY_REF_RECONCILER_GRACE_DAYS` | no | Fallback cleanup grace for legacy lifecycle records, bounded from `0` through `365`. Default `7`. |
| `MAC_REPOSITORY_REF_RECONCILER_REMOTE` / `MAC_REPOSITORY_REF_RECONCILER_BASE_REF` | no | Git remote name (default `origin`) and optional explicit `<remote>/<branch>` ancestry target. Without a base override, the remote HEAD is auto-detected. |
| `MAC_REPOSITORY_ACCESS_FAILURE_COOLDOWN_SECONDS` | no | How long a newest authentication/authorization failure excludes a reviewer for the matching project, repository host, and operation. Default `1800`. |
| `MAC_REPOSITORY_ACCESS_SUCCESS_TTL_SECONDS` | no | How long a successful repository-access learning receives reviewer-selection preference. Default `86400`. |
| `MAC_REVIEW_NUDGE_MAX_ATTEMPTS` | no | Maximum durable delivered verdict nudges for one review before it is retracted. Default `10`. |
| `MAC_SUPERVISOR_KIND` | no | Runtime supervisor selected by fleet deploy: `systemd`, `launchd`, or `supervisord`. |
| `MAC_MEMORY_TOPOLOGY_FILE` | no | Hermes-visible memory topology JSON. Default `~/.hermes/mac-memory-topology.json`. |
| `MAC_SHARED_SERVICES_MANAGER_AGENT` | no | Agent that owns hub-managed shared services. Defaults to the configured fleet hub. |
| `QDRANT_URL` / `QDRANT_ADDRESS` / `QDRANT_FLEET_URL` | no | Shared Qdrant level-2 memory endpoint. When set, Hermes startup readiness validates `/collections`. |
| `MAC_REQUIRE_QDRANT_MEMORY` | no | Set `1` to require shared Qdrant memory readiness. Fleet deploy enables this by default. |
| `MAC_QDRANT_MEMORY_ALLOW_DEGRADED` | no | Temporary operator override that allows startup when required Qdrant is missing or unreachable. |
| `QDRANT_PIDS_LIMIT` | no | Container PID/thread cap for the hub-managed Qdrant (supervisord wrapper + systemd unit). Default `4096`. Raise on very-high-core nodes; see Troubleshooting. |

Generate a secret key once:

```console
openssl rand -base64 48
```

Store it in a secrets manager. Rotating the key requires re-encrypting all
secret values; today this is a manual procedure (re-emit every secret with
the new key).

## Environment variable precedence

MAC resolves each `MAC_*` variable according to a three-level contract:

1. **Process environment wins.** A variable already present in the invoking process environment (e.g. set by the supervisor unit, injected by a secret manager, or exported by the operator shell) is used as-is and is never overridden by the env file.
2. **Env file supplies defaults.** A variable absent from the process environment receives its value from the operator env file (typically `~/.mac/.env` or the path given by `MAC_ENV_FILE`).
3. **Env file is the operator default store.** Operators should record stable deployment values — tokens, URLs, feature flags — in the env file. Runtime overrides belong in the process environment and are not written back to the file.

See [env-config-reference.md](env-config-reference.md) for the full variable catalog.

## Systemd

```console
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

```console
make setup
```

The wizard asks for the hub, agents, SSH targets, OS families, supervisors,
Slack home channel, per-agent Hermes model selectors, worker mode, canary
policy, Qdrant shared-memory endpoint, fleet network provider, and optional
hub token. It writes:

- `~/.mac/fleets.yaml`: home-scoped multi-fleet topology, keyed by hub node.
- `~/.mac/.env`: caller-machine deploy settings and local secrets, mode 0600.

To deploy after the wizard:

```console
make deploy HUB=<hub-node>
```

## Declarative Setup For Agents

LLM-driven setup should prefer a spec file over the interactive wizard. The
setup spec is validated before files are written, and the doctor output lists
missing env vars and next commands in machine-readable JSON.

Rather than hand-writing a spec, start from a generic, per-CSP sample. The repo
ships de-personalized samples under `deploy/fleet/samples/` (GKE is the worked
example); a real, named fleet spec lives **outside git** in
`~/.mac/specs/<fleet>.fleet.yaml`, created at install time by copying and
customizing a sample. Never check a named fleet into the repo.

```console
scripts/setup-fleet.py --list-samples                  # browse per-CSP samples
scripts/setup-fleet.py --init-from gke --name my-gke   # -> ~/.mac/specs/my-gke.fleet.yaml
$EDITOR ~/.mac/specs/my-gke.fleet.yaml                 # fill in the <placeholders>
make setup ARGS="--spec ~/.mac/specs/my-gke.fleet.yaml --force"
```

See `deploy/fleet/samples/README.md` for the per-CSP convention and the knobs
that differ per cloud (bastion/ProxyJump, network provider, in-cluster vs
public DNS, supervisor).

Example `fleet-setup.yaml`:

```yaml
schema: mac.fleet_setup.v1
fleet:
  name: horde
  hub: horde-hub
  hub_url: http://horde-hub:8789
agents:
  - name: horde-hub
    target: ubuntu@10.0.0.10:2201
    os: linux
    model: nvidia/llama-3.3-nemotron-super-49b-v1
    worker:
      mode: loop
  - name: horde-worker
    target: ubuntu@10.0.0.11
    os: linux
router:
  backend: inproc
  providers:
    - id: nvidia
      key_env: NVIDIA_API_KEY
network:
  provider: tailscale
```

Recommended LLM flow:

```console
export NVIDIA_API_KEY=...

mac fleet validate --spec fleet-setup.yaml
mac fleet doctor --spec fleet-setup.yaml
make setup ARGS="--spec fleet-setup.yaml --force"
```

`make setup ARGS="--spec ..."` writes `~/.mac/fleets.yaml` and `~/.mac/.env`,
then deploys the generated plan. To configure only:

```console
make setup ARGS="--configure-only --spec fleet-setup.yaml --force"
```

If a provider key such as `NVIDIA_API_KEY` is absent from both the environment
and the spec, validation fails before deployment so the fleet cannot silently
come up without chat routing.

The checked-in `deploy/fleet/config.yaml` is a generic sample only. It is
marked `sample: true`, and `deploy/deploy-mac-fleet.sh` refuses to deploy from
it unless `MAC_DEPLOY_ALLOW_SAMPLE_CONFIG=1` is set explicitly for tests.

## Reaching the Hub Node

The hub control plane binds to `hub_url` as declared in `~/.mac/fleets.yaml`.
How you reach it from a client machine depends on the network topology.

> **Client bootstrap:** `mac login` now combines strict host-key verification,
> a managed local tunnel, hub-local scoped enrollment, authenticated validation,
> and atomic profile installation. Do not copy the hub's `~/.mac`, admin token,
> `mac.db`, `MAC_SECRET_KEY`, provider keys, or SSH private keys to a new client.

For a directly reachable hub, the interim client setup is:

```console
export MAC_API_URL=https://mac.example.internal
export MAC_API_TOKEN=<scoped-client-token>
mac diagnostics
mac task stats
mac agent list
```

Use a mode-`0600` env file or a process environment supplied by a secret
manager. `--token` exists for automation/recovery but is not the preferred
interactive path because argv can be visible in shell history and process
inspection.

### Direct access (same network or VPN)

Hub is directly routable — no tunnel needed:

```console
# Confirm health
curl http://<hub-host>:8789/health

# Deploy
make deploy HUB=<hub-node>
```

### SSH port forward and scoped enrollment

Hub lives behind a bastion or inside a K8s cluster. Record the route in the
home-scoped fleet registry so deploy, credential recovery, snapshots,
migration, and Electron use the same resolver:

```yaml
# ~/.mac/fleets.yaml
fleets:
  my-fleet:
    hub_agent: hub
    control_port: 8789
    defaults:
      ssh_jump: horde@bastion.example.com:2222
      identity_file: ~/.ssh/mac-my-fleet
      ssh_known_hosts_file: ~/.ssh/mac-my-fleet-known-hosts
      ssh_host_key_policy: strict
    agents:
      - name: hub
        target: horde@my-hub.cluster.local
        os: linux
```

Provision and verify the hub and bastion host keys in `~/.ssh/known_hosts`
before opening the tunnel. Disabling host-key verification is not a supported
bootstrap substitute.

The normal client path is one command:

```console
mac login --fleet my-fleet --profile my-fleet --client-id my-laptop
mac login status --profile my-fleet
mac task stats
mac agent list
```

For a reproducible production profile the fleet route should resolve an explicit
identity file and a verified known-hosts/host-CA source; login validates and
pins whatever is supplied. An equivalent route can be given without a fleet
registry using `--ssh`, `--identity-file`, `--known-hosts-file`, optional
`--ssh-port`, and optional `--proxy-jump`. A directly reachable hub may use
`--host-key-fingerprint SHA256:...`; proxy-jump routes require a prepared
known-hosts file. Anything omitted falls back to OpenSSH's own resolution
(default identities/agent, `~/.ssh/known_hosts` with `accept-new`), so
`mac login --ssh <host>` works like `ssh <host>` — but such a profile then
depends on the enrolling machine's ambient ssh state rather than being fully
portable.

The commands below remain the low-level recovery procedure when diagnosing SSH
or enrollment independently.

```console
# Confirm the route is portable and does not depend on ~/.ssh/config.
mac fleet ssh-spec --fleet my-fleet --agent hub --portable --json

# Forward hub control port to localhost and open the UI
ssh -N -F /dev/null \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$HOME/.ssh/mac-my-fleet-known-hosts" \
  -o ProxyJump=horde@bastion.example.com:2222 \
  -i "$HOME/.ssh/mac-my-fleet" \
  -L 8789:127.0.0.1:8789 \
  horde@my-hub.cluster.local
# then: curl http://localhost:8789/health
# or:   open http://localhost:8789/ui
```

With that tunnel open, use a second verified SSH session to invoke enrollment
locally on the hub and stream the one-time token directly into the client
profile store:

```console
ssh -T -F /dev/null \
  -o BatchMode=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$HOME/.ssh/mac-my-fleet-known-hosts" \
  -o ProxyJump=horde@bastion.example.com:2222 \
  -i "$HOME/.ssh/mac-my-fleet" \
  horde@my-hub.cluster.local \
  'mac --json client enroll my-laptop \
    --fleet my-fleet --profile my-fleet \
    --api-url http://127.0.0.1:8789 \
    --scopes read,write,dispatch' \
  | mac client profile install -

mac --profile my-fleet diagnostics
mac --profile my-fleet task stats
```

The API hot-reloads `$MAC_HOME/client-principals.json`, so the new credential
works immediately. For normal operation, renewal rotates and validates the
bearer before replacing the local credential; revoking logout invalidates it
before deleting local state:

```console
mac login renew --profile my-fleet
mac logout --profile my-fleet --revoke
```

The equivalent low-level recovery commands are:

```console
ssh -T horde@my-hub.cluster.local 'mac --json client renew my-laptop' \
  | mac client profile install - --profile my-fleet

ssh -T horde@my-hub.cluster.local 'mac client revoke my-laptop'
mac client profile remove my-fleet
```

Use the explicit SSH options above for renewal and revocation as well. The
short form keeps the lifecycle example readable. See
[SSH Client Bootstrap Contracts](client-bootstrap-contract.md) for schemas,
file modes, failure rules, and legacy migration.

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

```console
# Hub is reachable at its Tailscale IP, e.g. 100.x.x.x:8789
curl http://100.x.x.x:8789/health
make deploy HUB=<hub-node>
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

```console
# Hub reachable at its headscale-assigned IP or MagicDNS name
curl http://hub.headscale.example.com:8789/health
make deploy HUB=<hub-node>
```

`MAC_DEPLOY_HEADSCALE_PREAUTHKEY` must be set in `~/.mac/.env`. With
`headscale.manage: true` the deploy script installs and configures the
headscale server on the hub node itself.

## One-Time ACC Replacement Deploy

For a configured fleet, use the Make deploy target:

```console
make deploy HUB=<hub-node>
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
Upstream provider credentials remain centralized on the hub, resolved by the
in-mac router from MAC's encrypted vault or inherited host-local environment;
spokes receive only their hub-facing MAC token.
Git-host credentials are a separate execution concern. Fleet deploy resolves
`MAC_DEPLOY_GH_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, then the operator's existing
`gh` keychain login, and writes the result to each managed runtime as
`GH_TOKEN`. Only the source name is logged; the value travels over SSH stdin,
not in the remote command. Pure `gateway_impl: none` workers require successful
GitHub validation by default before drain or source replacement. The variable
is also included in OpenShell's private mode-`0600` environment bundle so
confined tasks can clone and publish without copied host SSH keys. Do not put
the value in `~/.mac/fleets.yaml`, a fleet spec, task metadata, or source
control. A vault record by itself does not populate a worker environment;
deploy or the Kubernetes runner Secret must inject the corresponding
environment key.

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

```console
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

```console
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
the default review workflow. The default workflow prefers a healthy reviewer
that has never owned the task. If no independent reviewer is currently
eligible, it may assign the least-conflicted healthy review-capable agent and
records `reviewer_independence=fallback` plus the reason in task history and
observability. A newly available independent reviewer supersedes a pending
fallback review. Set task metadata `review.require_independent_reviewer: true`
(or `review.allow_independence_fallback: false`) for work that must wait instead.
Every path still requires a separate signed `review_verdict`; agent-generated
work still requires a reviewer LLM different from the executor LLM. The
workflow publishes/completes the task only when executor evidence and reviewer
verdict are verifiable.
Failed executions fail the task with evidence attached.

For high-risk work, set `metadata.review.risk_level` to `high` or `critical`.
Approval then fails closed unless the signed executor and verdict manifests
identify different model families and different upstream providers; merely
using two versions of Claude, GPT, or another single lineage is insufficient.
Unknown lineage/provider metadata also blocks approval, and reviewer
independence fallback is disabled. The same constraints can be enabled
individually with `review.require_different_model_family: true` and
`review.require_different_model_provider: true`.

```json
{
  "review": {
    "risk_level": "high",
    "require_independent_reviewer": true
  }
}
```

### Repository credential learning and reviewer routing

Every completed remote review preparation writes an authoritative
`fleet_learning:repository_access` memory record. The JSON content uses schema
`mac.fleet_learning.v1` and records only routing-safe facts: project,
repository host, operation, agent, credential source name, outcome, failure
class, bounded redacted error signature, recommendation, and task/review IDs.
It never stores a credential value or authenticated URL.

For the task's repository host, reviewer selection prefers agents with a
recent successful `review_clone`, then agents with no recent matching record.
An agent whose newest matching record is an authentication or authorization
failure is ineligible during the configured cooldown. A newer success restores
eligibility immediately; cooldown expiry returns the agent to unknown status.
This lookup reads SQLite directly, so routing changes immediately and does not
wait for Qdrant vector backfill.

Review Jobs receive optional Git-host keys from the runner's configured Secret
(default `mac-api-config`): `GH_TOKEN`, `GITHUB_TOKEN`, `GITEA_TOKEN`, and
`GITEA_USER`. Missing keys are allowed for public repositories. Review Jobs do
not receive `MAC_SECRET_KEY`. Host-mode workers also recognize
`MAC_TASK_GIT_TOKEN` as a fallback. The worker injects an HTTPS token only into
the Git command and restores the checkout's `origin` to the clean remote URL.

The pushed-ref evidence check uses the same environment-backed resolver. An
authentication, authorization, or network failure on the control-plane host is
indeterminate: it does not prove that the pushed ref is absent. A successful
lookup with no matching ref still rejects phantom-push evidence, and the
review verdict must still verify the executor's work.

Inspect the routing inputs and resulting review state:

```console
mac --json memory search \
  --record-type fleet_learning:repository_access \
  --order desc --limit 50

mac --json memory search \
  --subject-type agent --subject-id agent_... \
  --record-type fleet_learning:repository_access \
  --order desc --limit 20

mac --json task show task_...
```

Do not hand-edit or invent success records to force routing. Repair the agent's
credential, run a real repository operation, and let the resulting success
supersede the failure. Multiple success records can exist when a review is
retried; only the newest matching record controls eligibility.

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

```console
curl -X POST 'http://hub.example.internal:8789/reviews/default/tick?limit=100&actor=operator'
```

Before enabling executor-backed claiming, use `dry-run` mode to record routing
candidates without creating leases:

```console
MAC_DEPLOY_WORKER_MODE=dry-run
MAC_DEPLOY_WORKER_REQUIRE_CANARY=1
MAC_DEPLOY_WORKER_ALLOWED_PROJECTS=mac-canary
```

Dry-run mode emits `worker.routing.policy`,
`worker.routing.dry_run_candidate`, or `worker.routing.no_candidate` events
into `/observability` and `/observability/stream`. It must show only synthetic
canary work before loop mode is enabled.

To enable executor-backed claiming from deploy config, set:

```console
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

## Legacy Beads Migration And Repository Registry

The live production issue authority is the MAC task ledger. Beads/Dolt
round-trip sync is not part of the normal deployment path, and operators should
not run `bd` for current MAC task lifecycle. Legacy Beads state is handled as a
one-time import path:

```console
mac task detect-beads <repo>
mac task migrate-beads <repo> --project <project>
```

Use `--tickets-only` only when creating local compatibility files during a
migration audit. `.tickets/<id>.md` is ignored local operational state; it is
not checked into this repository and is not a cross-host ledger.

Repository-backed execution uses the project repository registry instead of a
legacy bridge poller. Register or onboard repositories through the current
project/repository commands:

```console
mac project onboard <repo-url>                         # creates contract-authoring task
mac bridge repository register <name> <path> --project <project>  # after .mac/project.yaml exists
mac bridge repository repos
```

Every registered repository must include a repository runtime contract at
`.mac/project.yaml`. Registration and task creation use that contract to attach
the bootstrap command, canonical test command, supported host families,
canonical remote URL, and required evidence to new tasks. See
[Repository Runtime Contract](repository-runtime-contract.md).
If CodeGraph is available on the registering host, repository registration also
runs `codegraph init` after excluding `.codegraph/` through the checkout-local
`.git/info/exclude`.

The hub's repository-ref reconciler uses this registry as its complete workset.
The hub therefore needs filesystem and GitHub access to each enabled checkout;
automatic `prune` additionally requires the contract's `canonical_remote_url`.
Monitor it with `mac repo refs status` and trigger a one-shot audit with
`mac repo refs reconcile --mode audit`. See
[Managed Repository Ref Hygiene](repository-ref-hygiene.md).

The hub advances the default review/publication workflow from heartbeat when
`MAC_REVIEW_TICK_ON_HEARTBEAT=1` and `MAC_REVIEW_TICK_HUB_AGENT` is set to the
configured hub agent. The tick only moves tasks when required evidence,
reviewer verdicts, and publication targets are present; otherwise it records
explicit waiting reasons in observability.

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

```console
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

## Docker Engine / Moby

```console
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

```console
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

## Kubernetes (K8s-native topology)

For multi-replica `mac-api`, deploy the manifests under `deploy/k8s/`.
The Postgres cluster itself is **not** managed from this repo — bring
your own (CloudNativePG, RDS, Cloud SQL, vendor-managed, etc.) and
supply the DSN via the `mac-api-config` Secret. Likewise, ArgoCD
`Application` manifests are not shipped here; point one Application
per kustomize tree from your platform-config repo if you sync with ArgoCD.

```console
# 1. Create the namespace + operator-supplied Secret carrying the DSN
#    and bearer tokens (or apply your ExternalSecret).
kubectl create namespace mac
kubectl -n mac create secret generic mac-api-config \
  --from-literal=MAC_DATABASE_URL='postgresql://user:pass@host:5432/mac' \
  --from-literal=MAC_SECRET_KEY="$(openssl rand -base64 48)" \
  --from-literal=MAC_WORKER_TOKEN="$(openssl rand -hex 32)"

# 2. mac-api Deployment + Service (replicas: 2, no PVC).
kubectl apply -k deploy/k8s/mac-api

# 3. mac-k8s-orchestrator — claims ready tasks, creates one batch/v1
#    Job per claim, and reconciles stuck Jobs against mac-api lease state.
kubectl apply -k deploy/k8s/mac-runner
```

The full apply order and ExternalSecret wiring are documented in
[`deploy/k8s/README.md`](https://github.com/jordanhubbard/mac/blob/main/deploy/k8s/README.md). The persistence layer
is portable across SQLite and Postgres because every `mac-api` SQL
string is in SQLite dialect; the `PostgresStore` translates placeholders
and provides a `json_extract` SQL function shim so the ~50 service
modules need no per-backend branching. See
[`docs/k8s-native-rewrite-plan.md`](archive/field-notes/k8s-native-rewrite-plan.md) for the
Phase 3-5 roadmap.

## Troubleshooting

### Deploy aborts: "Qdrant did not become ready at :6333" (high-core nodes)

On a node where the container's CPU cgroup quota is small but the host exposes
many cores (a common Kubernetes shape — e.g. a 4-CPU pod on a 192-core node),
Qdrant sizes its actix/tokio thread pools from `/proc/cpuinfo` (the **full** node
core count, not the cgroup quota) and tries to spawn far more threads than the
container's PID cap allows. Thread creation then fails with `EAGAIN` and Qdrant
panics at startup:

```
ERROR qdrant::startup: Panic ... Cannot spawn Arbiter's thread:
  "actix-rt|system:0|arbiter:NNN": Os { code: 11, kind: WouldBlock,
  message: "Resource temporarily unavailable" }
```

`mac-qdrant-run` (or the systemd unit) exits immediately, supervisord/systemd
marks the program FATAL, and the deploy aborts because Qdrant is mandatory on
the hub. The fix ships by default: the container PID cap is now
**`QDRANT_PIDS_LIMIT` (default 4096)** instead of a hardcoded 512. If an even
larger node still hits this, raise it in the fleet env, e.g. `QDRANT_PIDS_LIMIT=8192`,
and redeploy. Quick check on the affected host: `--pids-limit=512` → panic /
`curl :6333/collections` returns 000; raised limit → `200`.

### supervisord pods: `supervisorctl` permission errors

On pods where `supervisord` runs as root (PID 1) with a root-only control socket
(`srwx------`), `supervisorctl` as the deploy user gets
`PermissionError ... xmlrpc.py`. The deploy already prefers `sudo supervisorctl`
(`run_supervisorctl`); ensure the deploy user has passwordless sudo. A bare
`supervisorctl ...` in a manual session needs `sudo`.

### Private repository review is retracted or remains in `reviewing`

Start with the task and shared learning records:

```console
mac --json task show task_...
mac --json memory search \
  --record-type fleet_learning:repository_access \
  --order desc --limit 50
```

Interpret the review reason before retrying:

- `reviewer_unavailable:reviewer_repository_access_authentication:<host>` means
  MAC learned that the selected reviewer could not authenticate, retracted it,
  and will prefer a recent successful peer. Repair that agent's host-specific
  token before expecting it to be selected during the cooldown.
- `reviewer_unavailable:reviewer_repository_access_authorization:<host>` means
  the credential was recognized but lacks access to the repository or
  organization. For GitHub organizations using SSO, authorize the token for the
  organization rather than rotating it blindly.
- `reviewer_unable_to_produce_verdict_after_<N>_attempts` means repository
  access may have succeeded but the reviewer executor did not emit a valid
  signed verdict. Inspect the review evidence and reviewer workspace; changing
  Git credentials is not the appropriate repair unless the memory record also
  reports an access failure.

The delivered-nudge cap prevents an unparseable or crashing reviewer from
spinning forever. It does not currently learn general executor/harness
failures as reviewer-routing exclusions; track and repair those separately.

## Dynamic model selection (opt-in, hub only)

The hub can periodically propose the fleet's "powerhouse" models from web-search
mentions, filtered against the configured providers' models.dev catalogs. It is
**opt-in** and does not control the default deployed router today: explicit
`MAC_ROUTER_DEFAULT_MODEL` and `MAC_ROUTER_WILDCARD_MODELS` values still win.
The first successful selection is stored as active because there is no incumbent;
later dynamic changes remain pending unless an enabled eval gate approves them or
an operator runs `mac fleet model-selection promote`.

Do not describe the current selection as proof that the router can serve a model.
The catalog namespace is not yet reconciled with the router's exact model allowlist,
and the per-worker strength ladder is not distributed from the hub. These gaps are
why deployment leaves both selection and its automated swap evaluator disabled by
default.

Environment variables (hub):

| Variable | Default | Meaning |
|----------|---------|---------|
| `MAC_MODEL_SELECT_ENABLED` | off | Run the weekly selection refresher. |
| `MAC_MODEL_SELECT_INTERVAL_SECONDS` | 604800 | Refresh cadence. |
| `MAC_MODEL_SELECTION_FILE` | `$MAC_HOME/model-selection.json` | Where the active/pending selection + strength ladder are persisted. |
| `MAC_MODEL_SWAP_EVAL_ENABLED` | off | Evaluate later swaps through the configured router and automatically adopt an approved candidate; otherwise swaps stay pending for `mac fleet model-selection promote`. |
| `MAC_MODEL_SWAP_EVAL_GOLDEN_SET` | built-in floor set | Path to a JSON or JSONL golden set of eval cases. |

The built-in floor includes paired benchmark-labelled and production-shaped
agentic-integrity cases for fabricated work, test tampering, score
falsification, and tool-result concealment. Every pair uses the same behavioral
requirement with `pair_id` plus `presentation` (`benchmark` or `realistic`). The
swap gate records `realism_gap`, the mean absolute score gap within complete
pairs, and blocks candidates whose gap increases by more than the configured
drift threshold. Integrity requirements belong in `safety_required_points`;
each entry may be a string or a list of acceptable phrasings. Missing one is a
safety violation and forces that case's correctness to zero.

The built-in cases are a smoke-test floor, not a deployment-quality corpus.
Configure a version-controlled, rotating, production-shaped holdout that covers
the fleet's actual tool, repository, evidence, and reviewer workflows. Keep a
private portion out of model-facing prompts, preserve benchmark/realistic pair
IDs, and review both aggregate quality and the realism gap before promotion.

Opaque hosted-model APIs expose outputs, not residual-stream activations, so
MAC cannot honestly run a Jacobian lens or any other model-internal activation
audit through the current router. MAC's optional **external activation probe**
only classifies tensors supplied by a model runtime that the operator owns and
instruments; it does not recover hosted-model states. If a future open-weight
backend exposes compatible residuals, the probe must remain advisory and use a
separately validated classifier plus a held-out calibration set. Deterministic
evidence, test, CodeGraph, review-diversity, and publication gates remain
authoritative; an activation classifier must never approve work by itself.
See [External activation-probe prototype](activation-probe/prototype-report.md)
for its exact data boundary and non-goals.

Per task, `--model <name>` pins a model by name and `--model-strength 1..10`
pins by capability (resolved via the strength ladder; **hub agent only** until
ladder distribution lands).

## Autonomous scientific optimization

The hub can continuously test bounded execution-policy changes against task
quality, rework, latency, tokens, and cost. The durable experiment registry,
database-backed singleton scheduler, mandatory delayed-quality guardrails, and
promotion/rollback workflow are documented in
[scientific-optimizer.md](scientific-optimizer.md). New systemd deployments
enable the scheduler in `mac.env.example`; set
`MAC_SCIENTIFIC_OPTIMIZER_ENABLED=0` during a staged rollout if the hub should
collect no new experiment assignments.

## Known limitations

- Dynamic model selection is opt-in and does not override the explicit router
  defaults installed by fleet deployment. Catalog/allowlist reconciliation and
  ladder distribution must land before it becomes a fleet routing control.
- SQLite topology is single-writer. Use the Kubernetes + Postgres
  topology for multi-replica deployments.
- No built-in TLS. Put a reverse proxy in front.
- `MAC_SECRET_KEY` rotation is manual.
- Operational success-first routing is currently enforced for repository access
  during autonomous review. General executor, sandbox, and verdict-production
  failures are bounded and visible but are not yet learned as reviewer-routing
  exclusions.
- Live SQLite → Postgres data migration tool is not yet shipped (K8s
  Phase 3.7 — pending). Greenfield Postgres deploys start with an
  empty schema applied automatically by `PostgresStore.initialize()`;
  existing SQLite deployments upgrading to the K8s topology must
  currently re-create state.
