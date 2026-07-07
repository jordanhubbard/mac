# mac

Multi-agent coordinator control plane.

`mac` is a clean-room control plane for fleets of AI agents. It is designed to
sit underneath a human-facing agent runtime such as
`NousResearch/hermes-agent`, OpenClaw, or a compatible system.

The human-facing runtime owns conversation, personality, adaptive memory,
skills, and messaging gateways. `mac` owns durable operational truth: tasks,
leases, routing, reviews, evidence, secrets, runtime manifests, rollout state,
and audit trails. Fleet deployments use stock OpenClaw in OpenShell for the
human-channel role; internal agents may share a stable public identity.

The goal is to let a user talk to a persistent Hermes agent with a real
personality and memory, then let that agent create durable work that a broader
fleet can execute, review, publish, and recover.

If you are new to the project, start with the
[MAC Quickstart](docs/getting-started.md). It explains the idea, vocabulary, and
first local commands before fleet deployment.

## Acknowledgements and Lineage

`mac`'s control-plane code is clean-room work, but the system is not built in a
vacuum. It has learned from, interoperates with, or substantially relies on the
following projects. The relationship is stated explicitly so that an
integration or protocol influence is not mistaken for copied source:

- **[Hermes Agent](https://github.com/NousResearch/hermes-agent) — vendored
  runtime:** `src/mac/_hermes` is a pruned, MAC-modified snapshot of Hermes
  Agent 0.15.1 at
  [`b1a25404b`](https://github.com/NousResearch/hermes-agent/commit/b1a25404b638bfbd79ce4d08b49afc0ee1361528).
  It supplies the agent loop, gateways, tools, plugins, and skills. See
  [ADR 0001](docs/adr/0001-unify-hermes-runtime-into-mac.md) and the
  [snapshot contract](deploy/hermes/SNAPSHOT.md).
- **[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) — execution
  security foundation:** MAC's agent process trees, filesystem/network policy,
  sandbox lifecycle, and normalized action-event collection integrate with
  OpenShell rather than reimplementing its isolation layer.
- **[NVIDIA NemoClaw](https://github.com/NVIDIA/NemoClaw) — reference
  integration:** NemoClaw remains compatibility and design reference material;
  it is not the implementation behind MAC's `openclaw` deployment mode.
- **[OpenClaw](https://github.com/openclaw/openclaw) — conversational gateway
  runtime:** MAC deploys a pinned stock OpenClaw image inside a MAC-authored
  OpenShell policy for always-on chat channels while MAC remains the durable
  task and fleet control plane. See
  [OpenClaw public identities](docs/openclaw-identities.md).
- **[OpenAI Codex](https://github.com/openai/codex) — coding executor:** MAC
  installs and invokes the Codex CLI for repository-editing workers inside its
  evidence and sandbox gates.
- **[OpenCode](https://github.com/anomalyco/opencode) — coding executor and
  reviewer:** MAC's Kubernetes runner includes OpenCode build and independent
  review paths, wrapped by MAC-owned test, evidence, and publication gates.
- **[CodeGraph](https://github.com/colbymchenry/codegraph) — code intelligence
  and evidence:** repository analysis, affected-test selection, and the
  mandatory source-change audit use CodeGraph's local index and CLI.
- **[NVIDIA NeMo Relay](https://github.com/NVIDIA/NeMo-Relay) — optional
  observability:** MAC maps request, task, tool, and model activity into Relay
  scopes when the `relay` extra is enabled.
- **[xterm.js](https://github.com/xtermjs/xterm.js) — vendored terminal UI:**
  the legacy dashboard bundles xterm.js and its fit addon under
  `src/mac/ui/vendor/xterm`; their MIT license texts are retained there.
- **[Qdrant](https://github.com/qdrant/qdrant) — shared vector memory:** fleet
  deployment uses Qdrant as the hub-managed level-2 semantic-memory service.
- **[Firecrawl](https://github.com/firecrawl/firecrawl) — API compatibility
  target:** `mac-firecrawl-gateway` implements the Firecrawl v2 request/response
  surface expected by Hermes. It is a clean-room compatibility gateway, not a
  vendored copy of Firecrawl.
- **[Beads](https://github.com/gastownhall/beads) — task-ledger prior art and
  migration source:** MAC learned from Beads' durable agent-task workflow and
  retains one-way Beads import tooling. The independent `mac task` ledger is
  now authoritative; MAC does not run Beads or Dolt.
- **[Agent Client Protocol](https://github.com/agentclientprotocol/agent-client-protocol)
  and [A2A](https://github.com/a2aproject/A2A) — interoperability standards:**
  MAC implements these specifications for editor-to-agent and agent-to-agent
  communication; these are protocol implementations, not vendored SDKs.
- **[Superpowers](https://github.com/obra/superpowers) — engineering-method
  influence:** plans and design specs under `docs/superpowers` were developed
  with its agentic planning and execution workflow; it is not a runtime
  dependency.

This is a direct-lineage and architectural acknowledgement, not an exhaustive
transitive dependency list. Python and Node dependencies remain documented in
their manifests and lockfiles; additional skill- and plugin-level notices are
kept with the vendored Hermes files that require them. Each upstream project
remains subject to its own license.

## Core Contracts

This project provides durable contracts for coordinating a fleet:

- SQLite-backed task ledger with state transitions, leases, history, evidence, dependencies, and recovery.
- Machine and agent registry with capabilities, resources, health, and availability.
- Dispatcher that matches open work to healthy capable agents and accounts for
  tenant pool policy, resources, capacity, stale heartbeats, and expired leases.
- Structured agent message bus that rejects arbitrary execution payloads.
- Typed AgentBus streams for ordered agent-to-agent JSON, text, or base64
  content chunks with NDJSON tailing semantics.
- Review and publication pipeline that binds each approval to the current
  executor evidence, requires an independent approved review of that exact
  attempt, and records publication hashes when policy requires them.
- Canonical durable task ledger (`mac task`) — a beads-equivalent that avoids the
  beads/dolt sync problems. `.tickets/<id>.md` is optional, gitignored local
  compatibility output for human/IDE viewing when a `.tickets/` directory already
  exists; it is not cross-host sync and is not authoritative.
- Dispatch staging is explicit in the task ledger: `mac task create
  --no-dispatch` writes `metadata.no_dispatch: true`, ready/dispatch skip that
  task, and `mac task release` clears that metadata key. Absence of the key is
  the dispatchable state; project-level dispatch pause is a separate gate.
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
- Managed repository-ref lifecycles that distinguish superseded work from
  deferred or failed attempts, plus a hub-owned recurring reconciler that
  retires only exact-SHA eligible refs after a grace period.
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
# See every supported lifecycle target. Bare `make` prints the same help.
make help

# Install/link the CLI and prepare the canonical Fleet IDE.
make install

# Verify the checkout, or log in and launch the GUI against that hub.
make test
mac login
make run-gui
```

`make test` runs the complete hermetic pytest suite with statement, branch, and
Python-subprocess coverage for MAC-owned `src/mac` code. Vendored Hermes
internals under `src/mac/_hermes` are excluded. Coverage is a regression safety
floor rather than a target for generating tests; see
[the test portfolio strategy](docs/testing-strategy.md). Use `make coverage`
for the same full-suite report, `make test-portfolio` to audit redundant
execution, and `make fault-replay` to prove tests detect known historical bugs.

The common lifecycle is deliberately conventional:

```bash
make install       # CLI + canonical Fleet IDE
make build         # Python wheel + production IDE bundle
make clean         # generated artifacts only
make distclean     # also remove .venv and node_modules
```

Use `make install-cli` or `make install-gui` when only one surface is needed.
Installation requires Python 3.11+, Git, GitHub CLI (`gh`), npm, and CodeGraph;
build and test targets also require `uv`.
Every source-consuming build, install, run, and test target refreshes CodeGraph
first; the installed pre-push hook does the same. Fleet configuration/deployment
is intentionally separate under `make setup` and `make deploy`.

For local SQLite/API development after installation:

```bash

# Required: a 32+ char secret used to derive the Fernet key for the secrets table.
# Without it, the CLI and API both refuse to start.
export MAC_SECRET_KEY="$(openssl rand -base64 32)"

uv run mac --db mac.db init
MAC_DB="$PWD/mac.db" uv run uvicorn mac.api:app --reload
uv run mac-hermes --url http://127.0.0.1:8000 --help
```

For standalone local work, pass `--db path/to/file.db`. `MAC_DB` is server
configuration; it does not implicitly opt CLI commands into direct SQLite
access. Without `--db`, the CLI selects a configured hub (`--hub-url`,
`MAC_API_URL`, `MAC_URL`, `MAC_HUB_URL`, or `~/.mac/fleets.yaml`) and otherwise
refuses to run instead of silently creating a stray `./mac.db`. On a deployed
hub, where `MAC_DB` and `MAC_HUB_URL` are both present, operator commands use
the HTTP API.

Control-plane server startup likewise requires either an explicit `MAC_DB`
SQLite path or `MAC_DATABASE_URL` Postgres DSN. It never creates
`~/.mac/mac.db` implicitly; that client-side path is inspected only by the
legacy local-ledger migration command.

### Control-plane authority is not repository-local task storage

`--db` selects a **direct SQLite authority**. It is appropriate for a standalone
development database, tests, explicit migrations, and stopped-hub maintenance;
it is not an offline cache, and its tasks are never uploaded, merged, or
reconciled with a remote hub. Direct access to `~/.mac/mac.db` or a deployed
hub's configured `MAC_DB` therefore requires `--local-authority`. MAC also probes
the configured local health endpoint and refuses that maintenance mode while
the hub is running.

Normal operations against a running hub omit both flags:

```bash
mac task stats
mac task show task_...
```

For exceptional SQLite maintenance, stop `mac-api` first, preserve its existing
`MAC_DB` environment, and make the override explicit:

```bash
mac --local-authority task stats
```

Restart the hub before resuming fleet work. Opening an existing database through
this direct path does not run schema DDL. Schema creation and additive migrations
run at control-plane startup or through an explicit `mac --db <path> init`.

The hub ledger is intentionally not rooted in one checkout. It coordinates
leases, agents, reviews, workflows, evidence, A2A work, non-repository
operations, and tasks spanning multiple repositories. A task reaches source
through its project repository registration and execution contract.

Fleet deployment assigns `MAC_CONTROL_PLANE_ROLE=hub` only to the selected hub.
That host receives the explicit database configuration and runs `mac-api`.
Spokes have `MAC_CONTROL_PLANE_ROLE=client`, no `MAC_DB`, and register their
workers and Hermes identities through the hub API. Deployment archives an
inactive legacy spoke database; if it contains active tasks, deployment stops
and requires `mac migrate local-ledger` rather than stranding the work.

GitHub issues remain external project-planning records. `.tickets/` is ignored
local migration/compatibility state, not another execution authority. Bridges
may create a hub task from an external issue, but the resulting `mac task`
record in the selected control-plane authority is what agents claim and run.

If an operator client already has active tasks in `~/.mac/mac.db`, inspect and
transfer them with `mac --json migrate local-ledger`, then execute against a
selected hub profile with `mac --profile <name> migrate local-ledger --execute`.
The command verifies hub copies before cancelling local records and
removes the live database only after creating a checked archive and manifest.
See [Local Ledger Authority Transfer](docs/local-ledger-migration.md).

### Client bootstrap status

`mac login` bootstraps a new client from verified SSH access to the hub. It
pins the host identity, opens the hub-local API tunnel, requests an independently
revocable scoped credential over SSH, validates that credential through the
tunnel, and only then atomically installs the local profile and mode-`0600`
secret. Managed tunnels reconnect on the next profile-backed command if their
SSH process has exited.

```bash
mac login --ssh mac@hub.internal \
  --identity-file ~/.ssh/mac-production \
  --known-hosts-file ~/.ssh/mac-production-known-hosts \
  --fleet production --profile production --client-id my-laptop

mac login status --profile production
mac task stats
mac agent list
mac login renew --profile production
mac logout --profile production --revoke

# Directly reachable scoped endpoint (automation/operator provisioning):
export MAC_API_URL=https://mac.example.internal
export MAC_API_TOKEN=<scoped-client-token>
mac diagnostics
mac task stats

# Shared-admin recovery for an existing operator workstation only:
mac fleet sync-token --fleet my-fleet
mac --fleet my-fleet diagnostics
```

Prefer environment or mode-`0600` files over `--token`, which can expose a
token through shell history or process inspection. `fleet sync-token` copies
the historical administrator token; do not use it for routine new-client
onboarding. See [SSH Client Bootstrap Contracts](docs/client-bootstrap-contract.md)
and [Production Deployment](docs/production-deployment.md#reaching-the-hub-node).

### Repository branch hygiene

Task cancellation records why its worker branches should be preserved or may
eventually be removed. Missing dispositions default to `preserve`; duplicate
and superseded work must name the replacement task. Audit and prune are
fail-closed, and manual prune is read-only unless `--execute` is explicit. Fleet
deployment enables a daily `prune` reconciler on the hub only; the runtime
default remains `off` for standalone API processes and stateless replicas.

```bash
mac task close task_old --cancelled --disposition superseded \
  --replacement-task task_new --reason "replacement published"

git fetch --prune origin
mac --json repo refs audit --repo .
mac --json repo refs prune --repo .          # dry-run
mac --json repo refs prune --repo . --execute --actor operator

# Hub-wide automatic reconciler status and immediate admin-triggered passes.
mac --json repo refs status
mac --json repo refs reconcile --mode audit --actor operator
mac --json repo refs reconcile --mode prune --actor operator
```

See [Managed Repository Ref Hygiene](docs/repository-ref-hygiene.md) for the
schedule/configuration, disposition policy, exact-SHA deletion contract, grace
periods, monitoring, and recovery.

## API

Run the REST API with `MAC_SECRET_KEY` and an explicit database set:

```bash
MAC_SECRET_KEY="..." MAC_DB="$PWD/mac.db" uv run uvicorn mac.api:app --reload
# or use factory mode to be explicit:
MAC_SECRET_KEY="..." MAC_DATABASE_URL="postgresql://..." \
  uv run uvicorn mac.api:create_app --factory --reload
```

Set `MAC_API_TOKEN` for one admin token, or `MAC_API_TOKENS` as JSON such as
`{"reader":["read"],"worker":["agent","dispatch"]}` to require scoped bearer
tokens. Hub-local `mac client enroll` writes independently revocable hashed
principals to `MAC_CLIENT_PRINCIPALS_FILE`; the API hot-reloads that registry.
With no static token or enrolled client configured, the local prototype API
remains open for development.

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

## Fleet IDE

The React + Monaco fleet IDE lives in `ide/`. From the repository root:

```bash
make install-gui
mac login
make run-gui
make build-gui
make package-gui
```

`make run-gui` starts Vite on `http://127.0.0.1:5273`. `make package-gui`
writes a static web bundle to `dist/mac-ide-web.tar.gz`. This is separate from
the maintenance-only Electron dashboard wrapper in `desktop/`. The existing
`ide-*` target names remain as compatibility aliases.

For local auth, `make run-gui` first reuses the active scoped client profile
created by `mac login`, including its managed SSH tunnel. In an interactive
terminal it then prompts for the target hub URL; press Enter for the profile
endpoint, enter another `http://` or `https://` host, or set `IDE_API_URL`
beforehand to skip the prompt. The credential stays inside the local Vite proxy
and is not exposed to browser storage. If no active profile exists, the launcher
sources `~/.mac/.env` and selects a token
without printing it. Set `IDE_FLEET=<fleet>` to prefer the matching
`MAC_API_TOKEN__<FLEET>` key; otherwise the launcher falls back to
`MAC_DEPLOY_HUB_TOKEN` and then `MAC_API_TOKEN`. The `make ide-run` compatibility
alias uses the same launcher.

## Tell Agents To Work On A Project

Agents work on dispatchable tasks, not on repositories by implication. The
operator flow is below. Use `--db` for local mode, or omit it when your CLI is
already pointed at a hub through `--hub-url`, environment, or `~/.mac/fleets.yaml`.

```bash
mac --db mac.db project onboard git@github.com:ORG/REPO.git --project my-project
# After the onboarding task has produced .mac/project.yaml and that contract
# exists in a hub-visible checkout:
mac --db mac.db bridge repository register my-project /srv/repos/my-project --project my-project
# or, for a non-repository/manual project:
mac --db mac.db project create my-project --active

mac --db mac.db task create "Fix failing tests" \
  --project my-project \
  --description-file desc.txt \
  --required-capabilities python

mac --db mac.db project activate my-project      # if the project was staged/paused
mac --db mac.db task release task_...            # if the task was created with --no-dispatch
mac --db mac.db dispatch tick --limit 10         # assign ready work now
```

Loop-mode agents claim matching work from `mac task ready`. For explicit manual
assignment, use `mac task claim <task_id> <agent_id>` followed by
`mac task start <task_id> <agent_id>` with the same transport flags.

## Current Task Workflow

For repository-backed work, the production path is:

1. A project's git repository is registered — e.g. via `mac project onboard
   <repo-url>`, which creates the contract-authoring onboarding task, followed
   by `mac bridge repository register <name> <path> --project <project>` once
   `.mac/project.yaml` exists in a hub-visible checkout. The mac task ledger is
   canonical; ready work is `mac task ready`.
2. Each task for a registered-repository project carries a repository contract,
   execution contract, and origin metadata, so the executor gets a real checkout.
3. A healthy worker claims the task, works only in a task-owned git worktree,
   renews its lease, and records command-audit rows for subprocesses.
4. The executor records typed evidence with a `mac.worker_evidence.v1`
   verification manifest. Repository work must report pushed/clean git state
   and passing checks before it can enter review.
5. Every review assignment path, including manual/API requests, enforces the
   same health, freshness, capability, target-policy, repository-access, tenant,
   persona, ownership, and cooperative-family checks. The policy is checked again
   when the reviewer submits a verdict so an assignment cannot outlive revoked
   eligibility. The default workflow first picks an eligible reviewer-capable
   agent that has not owned the task or participated in its cooperative work
   family. If none exists, it progressively relaxes only those independence
   preferences and records the fallback; health, capability, tenant,
   repository-access, signed-verdict, and cross-model gates remain mandatory.
   Set `review.require_independent_reviewer: true` in task metadata when a task
   must wait rather than use this fallback. For remote repositories it consults shared
   `fleet_learning:repository_access` memory: a recent successful clone is
   preferred, while a newer authentication or authorization failure makes that
   agent temporarily ineligible for the same project, host, and operation.
6. Review workers resolve Git-host credentials from their environment for the
   individual Git command, immediately restore a credential-free `origin`, and
   record a secret-free success or failure learning. An authentication failure
   triggers immediate reviewer re-evaluation instead of repeating the same
   credential pattern.
7. The workflow waits for signed `review_verdict` evidence targeting the
   immutable executor evidence selected when the attempt entered review. The
   semantic reviewer owns the verdict; independent build/test/CodeGraph checks
   may veto an approval but cannot reverse a rejection. Review nudges are capped
   by durable delivered attempts, so a reviewer that cannot produce a verdict
   is retracted instead of being nudged indefinitely.
8. Publication completes the mac task. Failed tasks are reopened with a bounded
   retry policy; exhausted retries remain failed and visible.

mac records the complete ledger in task history, evidence, reviews, publications,
command audit, observability, and notifications — all in the mac task store,
which is authoritative. Lease renewals stay internal to avoid noise.

Repository-access failure cooldown defaults to 30 minutes
(`MAC_REPOSITORY_ACCESS_FAILURE_COOLDOWN_SECONDS=1800`), successful access is
preferred for 24 hours (`MAC_REPOSITORY_ACCESS_SUCCESS_TTL_SECONDS=86400`), and
review verdict nudges default to 10 delivered attempts
(`MAC_REVIEW_NUDGE_MAX_ATTEMPTS=10`). Stale workflow advancement reservations
are recovered after 60 seconds by default
(`MAC_WORKFLOW_ADVANCEMENT_RESERVATION_SECONDS=60`). See
[Fleet Operational Learning](docs/fleet-operational-learning.md) for the record
schema, credential boundaries, inspection commands, and failure semantics.

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

Workflow runs are deliberately single-current-node routing state machines, not
parallel fan-out engines. Cooperative fan-out uses task children and
dependencies: each child is assigned to a distinct executor, completed child
evidence contributes immutable commit/ref inputs to the reopened parent, and a
different integration executor must merge every exact child commit and
verify the combined result before the parent can enter review.

Node advancement uses a durable reservation containing the preallocated next
task ID. Task creation, workflow linkage, and task-created history commit
atomically; a stale reservation can therefore recreate or adopt that same task
without duplication. Workflow history for pre-decided approval chains is staged
until the downstream task and run transition finalize together. Cancelling a run
commits the run state, current-task cancellation, history, and transition outbox
atomically, so the cancellation callback cannot create downstream work.

Dispatcher recovery reads bounded pages selected by the indexed
`workflow_runs.next_action_at` deadline; it does not scan workflow-definition
JSON to discover timeouts. A malformed run records a run-scoped failure and
does not stop later candidates. Default-review sweeps query only `needs_review`
and `reviewing` rows. Autonomous workflow/review cursors and short leases live
in `reconciliation_state`, so traversal survives restarts and competing hub
replicas do not process the same page.

Each dispatch tick performs one bounded expired-lease page, one bounded
blocked-task page, and one bounded task/agent inventory pass before assigning a
batch. Dead-letter output is bounded too; `/dispatch/dead-letters/page` exposes
its opaque `next_cursor` for complete traversal.

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

# Inspect secret-free repository-access outcomes used by reviewer routing.
mac --db mac.db --json memory search \
    --record-type fleet_learning:repository_access --order desc --limit 50

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
- [SSH Client Bootstrap Contracts](docs/client-bootstrap-contract.md)
- [Repository Runtime Contract](docs/repository-runtime-contract.md)
- [Managed Repository Ref Hygiene](docs/repository-ref-hygiene.md)
- [Fleet Operational Learning](docs/fleet-operational-learning.md)
- [OpenClaw public identities](docs/openclaw-identities.md)
- [Review-strategy experiments](docs/review-strategy-experiments.md)
- [Integration Authority Contract](docs/integration-authority-contract.md)
- [Soul Preservation Runbook](docs/soul-preservation-runbook.md)
- [Scaling Plan](docs/scaling-plan.md)
