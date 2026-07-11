# Stock OpenClaw gateway under MAC OpenShell policy

This is MAC's primary chat-gateway implementation. It runs the official stock
OpenClaw image inside a MAC-owned OpenShell sandbox. `deploy/nemoclaw/` remains
reference material and is not invoked by this path.

Task execution is a separate MAC worker role. It uses an authenticated coding
agent inside the task OpenShell sandbox and fails closed when no verified route
exists. Reflection and ACP agent turns enter this OpenClaw sandbox through the
host `openclaw-agent` wrapper; no path falls back to Hermes chat.

## Pinned runtime

- OpenClaw: `2026.6.11`
- Source image: `ghcr.io/openclaw/openclaw:2026.6.11`
- Manifest digest: `sha256:3814fb1f62f9cfc5944de088c5817c68c88b5d721feebe36420b666a90a61ce7`
- Local image: `localhost/mac-openclaw:2026.6.11-mac.10`
- OpenShell: the MAC fleet pin installed by `deploy/openshell/bootstrap-openshell.sh`

The official image is extended only with OpenShell's required non-root
`sandbox` identity, Bash contract, MAC's state-directory layout, and the
repo-owned `mac-continuity` plugin.

## Identity and memory continuity

The host is authoritative for the transferable OpenClaw trees:

- `~/.mac/openclaw/workspace` — SOUL, IDENTITY, USER, MEMORY, daily/history
  memory, and migrated skills;
- `~/.mac/openclaw/state` — OpenClaw sessions, device identity, plugin state,
  cron state, and indexes;
- `~/.mac/openclaw/migration` — source hashes, counts, redaction totals,
  conflicts, cron conversion plan, and personality provenance;
- `~/.mac/openclaw/archive` — the two most recent pre-restart checkpoints.

When `~/.hermes` exists, `migrate-hermes-continuity.py` imports its identity,
authoritative `memories/USER.md` and `memories/MEMORY.md`, text conversation
history, skills, and enabled/disabled cron definitions. It never modifies or
deletes the Hermes source. High-confidence credentials are redacted before any
file enters the OpenClaw workspace; binary skill assets containing credential-
like bytes are withheld. Locally edited OpenClaw files win over a later Hermes
re-import and the new candidate is recorded as a private conflict.

When `~/.hermes` is absent, an already configured OpenClaw workspace is also
authoritative. Only when both are absent does fleet deploy ask a reachable,
established agent to propose a unique roster-aware name, role, vibe, SOUL,
starter USER, and starter MEMORY. The proposal is schema/duplicate/secret
validated and its mentor provenance is persisted before non-interactive setup.
Deployment fails closed if no mentor can produce a valid proposal; MAC never
silently installs the stock blank wizard identity.

OpenClaw's native keyword and session memory search covers the workspace and
OpenClaw sessions. The `mac-continuity` prompt hook additionally fetches the
agent's current MAC mood plus agent-scoped medium and long memories before each
turn. Its tools let the agent recall those memories and self-report, read, or
clear its own mood; bound agent tokens cannot act on a peer.

The image also ships `/usr/local/bin/curiosity` and a host wrapper at
`~/.mac/bin/curiosity`. A six-hour local OpenClaw cron performs the continuous
curiosity pass. Candidate hypotheses, evidence, counterevidence, provenance,
and falsifiable tests remain quarantined under durable OpenClaw state. The
sidecar records every transition in a hash-chained provenance ledger and never
writes a candidate to workspace memory until an explicit `curiosity approve`
includes an actor, reason, and external approval ID. Agent tools can submit and
inspect candidates and invoke `curiosity abuse-frame`; they intentionally do
not expose approval. Existing Hermes curiosity trees are redacted and
preserved as untrusted migration material, never silently promoted.

`AGENTS.md` applies the evidence-bound Angry Librarian and Moral Clarity
postures to both migrated and newly mentored identities: challenge weak claims,
surface possible false equivalence when power or responsibility differs, and
direct protective anger toward preventing harm without dehumanization.

## Host-local inputs

The installer reads deployed `~/.mac/mac.env` for fleet/router settings and the
owner-only `~/.mac/openclaw/credentials.env` for human-channel secrets. It does
not source `~/.hermes/.env`. The following values must be available through MAC
configuration or explicit `MAC_OPENCLAW_*` overrides:

- agent and Hermes-instance identity;
- MAC router URL, API key, and selected model;
- Slack Socket Mode bot and app tokens;
- one or more Slack and/or Telegram accounts assigned to the logical public
  identity hosted by this agent. OpenClaw owns multi-workspace residency
  natively; each configured account must resolve to a distinct workspace or
  bot identity.

During the first migration only, if OpenClaw has no channel-routing map yet,
the installer sanitizes and copies Slack account/channel IDs from the legacy
Hermes map into `~/.mac/openclaw/slack_home_channels.json`. The OpenClaw-owned
copy becomes authoritative and is never overwritten; no token or other legacy
field is imported.

Telegram long polling permits only one active gateway per bot token. Do not
copy one Telegram token to multiple public identities. Headless agents receive
no human-facing credentials and are represented by the fleet identity. For live channel-send validation,
also set `MAC_OPENCLAW_TELEGRAM_CANARY_TARGET` to that bot's operator chat ID.
Fleet deploy prefers identity-scoped MAC vault names such as
`channel-identity.<identity>.telegram.<account>.bot` and
`channel-identity.<identity>.telegram.<account>.canary_target`, validates the bot
with Telegram `getMe`, and materializes them in the OpenClaw-only, mode-0600
`~/.mac/openclaw/credentials.env`. They are intentionally not written to
`~/.hermes/.env`, which prevents the retained rollback gateway from competing
for the same Telegram long-poll stream.

Slack discovery includes every complete bot/app pair under the identity-scoped
namespace, with the older `slack.<agent>.<workspace>.*` names accepted during
migration. Tokens are exposed to OpenClaw only through account-specific
`MAC_OPENCLAW_SLACK_<WORKSPACE>_*_TOKEN` SecretRefs. Do not export the stock
`SLACK_BOT_TOKEN` or `SLACK_APP_TOKEN` names: OpenClaw interprets those as an
additional implicit `default` account and would connect twice to one workspace.

The generated `openclaw.json` contains SecretRefs and `${ENV}` references, not
credential values. Actual values are written only to
`~/.mac/openclaw/managed/runtime.env` with mode `0600`, then uploaded into the
sandbox. They are never passed through process argv.

## Prepare without changing services

```bash
deploy/openclaw/install-openclaw-gateway.sh prepare
```

This operation is idempotent. It runs the reversible continuity import, renders
the policy and configuration, builds the pinned image if absent, writes the
host wrapper, and preserves the existing gateway token. If an older OpenClaw
sandbox must be replaced, its legacy `/home` state is first staged through
OpenShell's transferable `/sandbox` root and merged into the owner-only host
state; differing files are retained under `~/.mac/openclaw/backups/`.

## Fleet deployment

Set `hermes.gateway_impl: openclaw` in the fleet configuration and deploy in
this order:

1. One non-hub physical worker as the canary.
2. The remaining non-hub physical workers.
3. Containerized worker and hub nodes.
4. The primary fleet hub last.

`deploy/deploy-mac-fleet.sh` supports systemd, supervisord, and launchd. It
starts OpenClaw, then requires all of these checks before disabling Hermes:

- `openclaw config validate --json`;
- runtime import of `mac-continuity`, all seven continuity/curiosity tools, and its
  `before_prompt_build` hook;
- curiosity ledger verification and an abuse-frame false-equivalence canary;
- forced native memory indexing and exact recall of the private per-agent
  continuity marker;
- authenticated in-sandbox `openclaw health --verbose --json` RPC health;
- live Slack and Telegram channel probes.

After those positive probes, deploy disables Hermes and NemoClaw and runs a
second, supervisor-specific finalization gate. That gate proves OpenClaw is
active and both legacy gateways are inactive. A service advertisement is not
published until this negative exclusivity proof passes.

For a canary, additionally export `MAC_DEPLOY_OPENCLAW_LIVE_CANARY=1`. The
verification command performs a real model turn and requires the sentinel
`MAC_OPENCLAW_CANARY_OK`, then sends a labeled canary message through both
Slack and Telegram. A live canary therefore also requires the configured Slack
home channel and `MAC_OPENCLAW_TELEGRAM_CANARY_TARGET`.

Successful sandbox verification first writes an owner-only pending record.
Only successful post-cutover finalization atomically publishes
`~/.mac/openclaw/service-advertisement.json`. The worker registers that record
as `resources.chat_gateway`, including the stock OpenClaw version, OpenShell
sandbox, sandbox-exec access method, Slack/Telegram transports, verification
time, supervisor states, and explicit exclusive-ownership proof.
The Fleet IDE displays the same record in the agent inspector. Rollback removes
the advertisement before restoring Hermes, so desired state is never reported
as a live service.

Hermes is disabled only after verification succeeds. It remains installed as
the rollback gateway until the separate Hermes-retirement chain completes. A
failed verification invokes rollback automatically.

## Manual verification

```bash
MAC_OPENCLAW_LIVE_CANARY=1 \
  deploy/openclaw/install-openclaw-gateway.sh verify
```

This command validates the sandbox and writes only
`verification-pending.json`; it does not claim exclusive ownership. Normal
fleet deploy stops the legacy services and invokes `finalize`. Do not invoke
`finalize` manually unless the supervisor cutover has already occurred: it
fails closed if OpenClaw is inactive or either legacy gateway is active.

Useful secret-free checks:

```bash
openshell sandbox get mac-openclaw-<agent>
openshell sandbox exec --name mac-openclaw-<agent> --no-tty -- \
  /bin/bash -lc 'set -a; . /home/sandbox/.config/mac-openclaw/runtime.env; set +a; exec /usr/local/bin/openclaw health --verbose --json'
```

On every worker start, `mac-agent-startup-self-test` independently requires the
exclusive advertisement, OpenShell confinement, readable OpenClaw model route,
and a `MAC_OPENCLAW_STARTUP_OK` model sentinel through `openclaw-agent`.

The gateway remains inside an OpenShell sandbox, and its container is still
recreated from the cached image on each service start because OpenShell 0.0.72
cannot safely reuse its forwarding/process boundary. The data is not
disposable: the stop wrapper downloads `/sandbox/workspace` and
`/sandbox/state`, atomically promotes both host checkpoints, then deletes the
container. Startup uploads the complete host trees. This guarantees one channel
consumer while retaining OpenClaw's own identity, sessions, outbox, plugin,
cron, and memory state across recreation. Task-execution sandbox reuse is a
separate policy.

## Rollback

```bash
MAC_OPENCLAW_FLEET_NAME=mac \
MAC_OPENCLAW_SUPERVISOR=auto \
  deploy/openclaw/install-openclaw-gateway.sh rollback
```

Rollback stops and disables the OpenClaw supervisor entry and restores the
retained Hermes gateway. It deliberately preserves the host OpenClaw workspace,
configuration, state, migration manifests, and checkpoints for diagnosis or a
corrected retry.

## Updating OpenClaw

Update all three pins together:

1. official tag and manifest digest in `OpenClaw.Containerfile`;
2. `OPENCLAW_VERSION` in `install-openclaw-gateway.sh`;
3. this runbook.

Run the focused deployment tests, stock OpenClaw config validation, a canary,
and the full repository contract before fleet promotion. Never replace the
digest with `latest` in a production path.
