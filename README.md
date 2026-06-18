# mac

Multi-agent coordinator control plane.

`mac` is a clean-room control plane for fleets of AI agents. It is designed to
sit underneath a human-facing agent runtime such as
`NousResearch/hermes-agent`, OpenClaw, or a compatible system.

Hermes owns conversation, personality, adaptive memory, skills, and messaging
gateways. `mac` owns durable operational truth: tasks, leases, routing,
reviews, evidence, secrets, runtime manifests, rollout state, and audit trails.

The goal is to let a user talk to a persistent Hermes agent with a real
personality and memory, then let that agent create durable work that a broader
fleet can execute, review, publish, and recover.

If you are new to the project, start with the
[MAC Quickstart](docs/getting-started.md). It explains the idea, vocabulary, and
first local commands before fleet deployment.

## Core Contracts

This project provides durable contracts for coordinating a fleet:

- SQLite-backed task ledger with state transitions, leases, history, evidence, dependencies, and recovery.
- Machine and agent registry with capabilities, resources, health, and availability.
- Dispatcher that matches open work to healthy capable agents and accounts for
  tenant pool policy, resources, capacity, stale heartbeats, and expired leases.
- Structured agent message bus that rejects arbitrary execution payloads.
- Typed AgentBus streams for ordered agent-to-agent JSON, text, or base64
  content chunks with NDJSON tailing semantics.
- Review and publication pipeline that requires typed evidence, independent
  approved review, and publication hashes when policy requires them.
- Human-facing Beads ledger mirroring for imported issue work: mac keeps its
  internal task history authoritative, and appends concise `mac-ledger v1`
  comments back to the Bead for imports, claims, state gates, evidence, review,
  publication, retry, and exhaustion milestones.
- Short-retention command audit for worker subprocesses so operators can see
  what agents actually ran without treating local shell history as evidence.
- Optional scoped API bearer tokens for read/write/agent/dispatch/secret/admin access.
- Tenant-scoped secret handles with audit records and redacted API/CLI output.
- Reproducible runtime manifests with stable digests and secret-value checks.
- Tenant, user, Persona, Hermes instance, and platform binding records for multi-user expansion.
- Project bridge, operational memory/provenance records, and gated rollout/rescue workflows.
- Repository runtime contract enforcement for registered project checkouts so
  agents can bootstrap and test work on macOS, Linux, WSL2, or narrower
  declared host families without relying on accidental local state.
- Role catalog, role assignment, provisioning requests, and data-driven DAG
  workflows that turn multi-step plans into durable tasks with per-node role
  requirements and run history.
- Evaluation contract: named `eval_sets` (scoring direction, baseline, regression threshold) and `eval_runs` against rollout versions, runtime environments, or agent builds; rollouts can require a passing `eval_run` before `promote`.
- FastAPI REST API and `mac` CLI.
- Hermes-side `mac-hermes` adapter for registration, sanitized task creation, status replies, and memory write-back payloads.

## Boundary With Hermes

Hermes is the primary interaction agent:

- Slack, Telegram, Discord, CLI, and other message apps terminate in Hermes.
- `SOUL.md`, `USER.md`, `MEMORY.md`, skills, and session memory belong to Hermes.
- Adaptive personality belongs to Hermes because it needs conversational context.
- Hermes may create work in `mac`, but should pass only the sanitized operational context needed by the task.

`mac` deliberately does not implement agent souls or personal memory. Its
`memory_records` are for operational provenance: imports, task evidence,
decisions, rollout events, and durable facts needed to audit work. User memory
and personality memory stay in Hermes.

Shared long-term recall is hub-managed infrastructure. The hub runs Qdrant for
shared level-2 memory, while each agent keeps its local Hermes soul,
conversation state, and private memory under `HERMES_HOME`. Fleet deploy writes
a Hermes-visible memory topology file that tells every agent where those
boundaries are and which hub endpoint owns shared services.

The identity framework reflects that split:

- `tenant`: an organization or isolated user deployment.
- `user`: a human identity inside a tenant.
- `persona`: a named Hermes personality with a `soul_ref` and `memory_scope`.
- `hermes_instance`: a running or durable Hermes identity such as `worker-1`.
- `platform_binding`: a Slack workspace/channel, Telegram chat, or similar binding.
- interaction task: a durable task created from a Hermes conversation with origin metadata, not copied private memory.

## Quick Start

```bash
python3 scripts/bootstrap-project.py
PATH=.venv/bin:$PATH .venv/bin/python -m pytest

# Required: a 32+ char secret used to derive the Fernet key for the secrets table.
# Without it, the CLI and API both refuse to start.
export MAC_SECRET_KEY="$(openssl rand -base64 32)"

uv run mac --db mac.db init
uv run uvicorn mac.api:app --reload
uv run mac-hermes --url http://127.0.0.1:8000 --help
```

The CLI stores state in `mac.db` by default. Use `--db path/to/file.db` or `MAC_DB` to choose a different SQLite database.

## API

Run the REST API with `MAC_SECRET_KEY` set:

```bash
MAC_SECRET_KEY="..." uv run uvicorn mac.api:app --reload
# or use factory mode to be explicit:
MAC_SECRET_KEY="..." uv run uvicorn mac.api:create_app --factory --reload
```

Set `MAC_API_TOKEN` for one admin token, or `MAC_API_TOKENS` as JSON such as
`{"reader":["read"],"worker":["agent","dispatch"]}` to require scoped bearer
tokens. With no API token configured, the local prototype API remains open for
development.

The built-in dashboard is served at `/ui`. Static dashboard assets are public so
the browser can load the shell, while data requests still use the same API token
rules as the REST API. Enter a token with the needed read/write/dispatch/secret
scopes in the dashboard when API tokens are enabled. The dashboard source is
plain TypeScript in `src/mac/ui/app.ts`; the checked-in `app.js` browser output
is served directly so there is no Node.js, npm, bundler, or frontend build step.
The dashboard has read models for overview, agents, task timelines, Hermes
activity, runtime/rollout status, observability metrics/logs, and redacted
secret audits. Operator actions cover dispatch ticks, task transitions,
evidence, reviews, publication, rollout advance/health/rescue, and secret
handle requests. It deliberately does not expose a casual secret reveal action.

Key route groups:

- `/tenants`, `/users`, `/personas`
- `/hermes-instances`, `/hermes-instances/{id}/context`, `/hermes-instances/{id}/work-context`, `/platform-bindings`
- `/dashboard/state`, `/dashboard/agents/{id}`, `/dashboard/tasks/{id}/timeline`, `/dashboard/dispatch/explain`, `/dashboard/hermes/{id}/activity`, `/dashboard/hermes/fleets/{id}/config-surface`, `/dashboard/hermes/fleets/{id}/config-surface/apply`, `/dashboard/rollouts/{id}/status`
- `/tasks`, `/tasks/{id}/evidence`, `/tasks/{id}/reviews`, `/reviews/default/tick`, `/publications`
- `/machines`, `/agents`, `/agents/{id}/heartbeat`, `/agents/{id}/claim-next`, `/dispatch/tick`, `/dispatch/dead-letters`
- `/roles`, `/agents/{id}/role`, `/agents/{id}/identity`
- `/provisioning/requests`
- `/workflows`, `/workflows/import-yaml`, `/workflows/seed`, `/workflows/{id}/start`, `/workflows/runs`, `/workflows/runs/tick`
- `/messages`
- `/agentbus`, `/agentbus/streams`, `/agentbus/streams/{id}/chunks`, `/agentbus/streams/{id}/events`
- `/agentbus/repo-update`, `/agentbus/artifact-publish`
- `/command-audit`, `/agents/{id}/command-audit`
- `/secrets`, `/secrets/{id}/access`, `/secrets/{id}/reveal`, `/secret-audits`
- `/runtimes`, `/runtime-runs`
- `/artifacts`, `/artifacts/{id_or_digest}` — canonical record for deliverables (kind, digest, uri, sbom_uri, signers); re-registering the same digest augments signers/metadata
- `/environments`, `/environments/{id}/deploy|current|deployments` — environment registry + artifact→environment deployment edges; deploy atomically retires the prior active deployment
- `/fleet/build-distribution` — aggregate live agents by `running_digest`; agents declare their build via `heartbeat`
- `/bridge/items`, `/memory`
- `/rollouts`, `/rollouts/{id}/artifact`, `/rollouts/{id}/health`, `/rollouts/{id}/rescue`
- `/eval-sets`, `/eval-sets/{id}/baseline`, `/eval-sets/{id}/events`, `/eval-runs`
- `/events` — unified audit stream across task/agent/project/fleet/rollout/eval_set/secret/environment/conversation_thread/vector_ref surfaces; filter by `subject_type`, `subject_id`, `actor`, `event_type`, `event_type_prefix`, `since`, `until`, `limit`
- `/observability`, `/observability/metrics`, `/observability/logs`, `/observability/summary`, `/observability/stream` — low-level metric/log ingestion, query, summary, and NDJSON subscription across API, control-plane, worker, Hermes, deploy, and external-agent layers
- `/notifications`, `/notifications/{id}/delivered`
- `/integrations/findings`, `/integrations/observations`
- `/agents/{id}/mood`, `/agents/{id}/mood/history` — agent-self-reported emotional state (warm/cheerful/sad/curt/cold/irritated/angry/enraged) with reason + optional TTL; transitions flow through `/events` as `subject_type=agent`
- `/agents/{id}/nap-schedule`, `/agents/{id}/nap-schedule/next`, `/nap-schedules`, `/nap-runs`, `/nap-runs/{id}/complete`, `/nap-runs/{id}/fail` — daily memory-consolidation lifecycle. Offset defaults to `md5(agent.name) %% 360` minutes (spreads the fleet across the 0–6h UTC window). mac coordinates `begin → DRAINING → complete/fail`; summarization and vector storage are off-process and linked via `evidence` + `vector_refs`.

## Current Task Workflow

For Beads-backed repository work, the production path is:

1. The hub polls registered Beads repositories and treats `bd ready --json`
   as canonical when it is available.
2. Each ready Bead becomes one durable mac task with repository contract,
   execution contract, origin metadata, and Beads provenance.
3. A healthy worker claims the task, works only in a task-owned git worktree,
   renews its lease, and records command-audit rows for subprocesses.
4. The executor records typed evidence with a `mac.worker_evidence.v1`
   verification manifest. Repository work must report pushed/clean git state
   and passing checks before it can enter review.
5. The default review workflow assigns a healthy reviewer-capable agent that
   has not owned the task, waits for signed `review_verdict` evidence, then
   publishes only after executor and reviewer evidence both verify.
6. Publication completes the mac task and syncs the backing Bead close. Failed
   mapped tasks are reopened with a bounded retry policy; exhausted retries
   remain failed and visible.

mac records the complete internal ledger in task history, evidence, reviews,
publications, command audit, observability, and notifications. For Beads-backed
work it also writes concise Beads comments with the prefix `mac-ledger v1` so a
human reading the Bead can see key milestones without opening the mac database.
Lease renewals stay internal to avoid noisy issue logs.

## Workflow Orchestration

mac has an API-level workflow system for turning an agentic multi-step plan
into durable tasks:

- Roles define required capabilities, prompts, optional hardware needs, and
  tenant scope. Agents can be assigned a role, and role assignment is checked
  against the agent's Hermes persona allowlist when one exists.
- Workflows are versioned DAGs of nodes and edges. Each node declares a required
  role and can add instructions, capabilities, approval behavior, or timeout
  policy.
- Starting a workflow snapshots the current definition, creates the first task,
  and stores the workflow linkage in task columns rather than trusting caller
  metadata.
- Terminal task transitions advance the run through matching edges, spawn the
  next task, or mark the run completed/failed/cancelled with append-only run
  history.
- Default seed data exists for bug, feature, UI, and self-improvement flows.

The REST API and CLI can create, import, seed, start, cancel, and tick workflow
runs today, and `/dashboard/state` includes workflow-run summary data for UI
clients. The checked-in dashboard does not yet provide a full visual workflow
authoring UI for humans to edit plans and answer all agent questions up front.

## CLI Examples

```bash
mac --db mac.db machine register workstation-1
mac --db mac.db agent register machine_... worker --capabilities python,review
mac --db mac.db tenant register personal
mac --db mac.db persona register tenant_... AssistantOne --soul-ref hermes://personal/assistant-one/SOUL.md --memory-scope hermes://personal/assistant-one/memory
mac --db mac.db hermes register tenant_... assistant-one --persona-id persona_... --home-ref hermes://personal/assistant-one
mac --db mac.db binding register tenant_... hermes_... slack T123/C456 --display-name "#ops"
mac --db mac.db interaction task hermes_... "Investigate deployment failure" --platform-binding-id binding_...
mac --db mac.db task create "Implement feature" --required-capabilities python
mac --db mac.db dispatch tick
mac --db mac.db task show task_...

# Secrets: prefer stdin or file input over argv to keep values out of shell history.
echo -n "$GH_TOKEN" | mac --db mac.db secret set github-token \
    --from-stdin --scopes '{"capabilities":["deploy"]}' --created-by human
mac --db mac.db secret set release-key --from-file ./release.key \
    --scopes '{"capabilities":["deploy"]}' --created-by human

# Rollouts require a pinned runtime and verified sha256 artifact before install.
mac --db mac.db runtime create mac-runtime \
    --manifest '{"image":"python:3.12@sha256:abc123","dependencies":["fastapi==0.111.0"]}' \
    --created-by human
mac --db mac.db rollout create 1.2.0 canary --runtime runtime_... \
    --artifact-uri artifact://mac/1.2.0 --artifact-hash sha256:abc123 \
    --health-policy '{"required_checks":["runtime","canary"]}' \
    --created-by human
mac --db mac.db rollout advance rollout_... start_canary --actor human
mac --db mac.db rollout health rollout_... \
    --checks '{"runtime":"healthy","canary":"ok"}' --actor monitor

# Evaluation: define a scored eval set, record runs against rollout versions,
# and gate promotion on a passing run.
mac --db mac.db eval set create task-success-rate \
    --scoring higher_is_better --baseline-score 0.90 --regression-threshold 0.02
mac --db mac.db eval run record evalset_... rollout_version 1.2.0 0.93
mac --db mac.db rollout create 1.3.0 canary --runtime runtime_... \
    --artifact-uri artifact://mac/1.3.0 --artifact-hash sha256:def456 \
    --required-eval-set-id evalset_... --created-by human
mac --db mac.db rollout advance rollout_... start_canary --actor human
# promote refused until a passing eval run exists for version 1.3.0
mac --db mac.db rollout advance rollout_... promote --actor human

# Unified audit stream: one query across task/agent/project/fleet/rollout/eval_set/secret/environment events.
mac --db mac.db events list --limit 50
mac --db mac.db events list --subject-type rollout --subject-id rollout_...
mac --db mac.db events list --prefix rollout. --since 2026-05-17T00:00:00+00:00
mac --db mac.db events list --actor monitor --event-type rollout.health_failure_during_rescue
mac --db mac.db observability list --layer control_plane --subject-type fleet

# Artifact registry + environment deployments + fleet build inventory.
mac --db mac.db artifact register image sha256:abc... artifact://mac/v1.2.0 \
    --created-by ci --sbom-uri sbom://mac/v1.2.0.spdx --signers ci,release-manager
mac --db mac.db env register staging --channel release --created-by human
mac --db mac.db env deploy staging sha256:abc... --actor release-bot
mac --db mac.db env current staging
mac --db mac.db agent heartbeat agent_... --running-digest <runtime-digest>
mac --db mac.db fleet build-distribution

# One-time ACC migration: dry-run first, then import open ACC work into mac.
# Claimed/in-progress ACC tasks are blocked unless explicitly requeued with --allow-active.
mac --db mac.db migrate acc ~/.acc/data/acc.db --mode dry-run \
    --report acc-migration-dry-run.json
mac --db mac.db migrate acc ~/.acc/data/acc.db --mode import \
    --report acc-migration-import.json

# Minimal worker harness: register/heartbeat first without claiming, then run
# an executor-backed claim/start/evidence/submit loop.
mac-agent --url http://hub.example.internal:8789 --register --agent-name worker-1 \
    --hostname worker-1.local --capabilities python,ops,review \
    --resources '{"capacity":2}' --heartbeat-only
mac-agent --url http://hub.example.internal:8789 --register --agent-name worker-1 \
    --capabilities python,ops,review --allowed-projects mac-canary --require-canary \
    --dry-run-claim
mac-agent --url http://hub.example.internal:8789 --agent-id agent_... \
    --workspace ~/.mac-agent/workspaces --allowed-projects mac-canary \
    --require-canary --executor ~/.mac/bin/mac-hermes-task-executor
mac-agent --url http://hub.example.internal:8789 --register --agent-name worker-1 \
    --capabilities python,ops,review --loop --workspace ~/.mac-agent/workspaces \
    --allowed-projects mac-canary --require-canary \
    --executor ~/.mac/bin/mac-hermes-task-executor

# Typed AgentBus: durable ordered content chunks; this is transport, not exec.
mac --db mac.db agentbus publish agent_sender --recipient-agent-id agent_recipient \
    --content-type application/vnd.mac.delta+json \
    --payload '{"kind":"delta","content":"hello"}'
mac --db mac.db agentbus read bus_... agent_recipient
```

Hermes-facing API adapter:

```bash
mac-hermes --url http://127.0.0.1:8000 register \
  --tenant personal \
  --persona AssistantOne \
  --instance assistant-one \
  --soul-ref hermes://personal/assistant-one/SOUL.md \
  --memory-scope hermes://personal/assistant-one/memory \
  --binding slack:T123/C456:#ops

mac-hermes --url http://127.0.0.1:8000 task hermes_... \
  "Investigate deployment failure" \
  --summary "The Slack deployment thread reports a failed publish step." \
  --platform-binding-id binding_... \
  --conversation-ref slack://T123/C456/1712345678.000100 \
  --required-capabilities ops

mac-hermes --url http://127.0.0.1:8000 reply task_...
mac-hermes --url http://127.0.0.1:8000 writeback hermes_... task_...
```

Fleet deployment reads generic defaults from `deploy/fleet/config.yaml` and
real topology from the home-scoped registry `~/.mac/fleets.yaml`. Run
`make setup` to create `~/.mac/fleets.yaml` and `~/.mac/.env`. Each fleet is
keyed by its hub node name; deploy with `make deploy HUB=<hub-node>`.
Fleet mesh networking is selected in that registry with `network.provider`;
`tailscale` is the default, while `headscale` is advanced opt-in and requires an
explicit login server, enrollment-key source, DNS assumption, and health check.

## Design Docs

- [MAC Quickstart](docs/getting-started.md)
- [Hermes Boundary](docs/hermes-boundary.md)
- [Hermes Integration](docs/hermes-integration.md)
- [Production Deployment](docs/production-deployment.md)
- [Repository Runtime Contract](docs/repository-runtime-contract.md)
- [Integration Authority Contract](docs/integration-authority-contract.md)
- [Soul Preservation Runbook](docs/soul-preservation-runbook.md)
- [Scaling Plan](docs/scaling-plan.md)
