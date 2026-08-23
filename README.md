# mac

Multi-agent coordinator control plane.

`mac` is a clean-room control plane for fleets of AI agents. It is designed to
sit underneath a human-facing agent runtime such as OpenClaw under OpenShell, NemoClaw Hermes, or a compatible system.

The human-facing runtime owns conversation, personality, adaptive memory,
skills, and messaging gateways. `mac` owns durable operational truth: tasks,
leases, routing, reviews, evidence, secrets, runtime manifests, rollout state,
and audit trails. Fleet deployments use stock OpenClaw in OpenShell for the
human-channel role; internal agents may share a stable public identity.

The goal is to let a user talk to an agent with a real
personality and memory, then let that agent create durable work that a broader
fleet can execute, review, publish, and recover.

If you are new to the project, start with the
[production documentation book](docs/index.md) or read the
[versioned HTML edition](https://jordanhubbard.github.io/mac/). It begins with
the system model and local operation, then advances through fleet deployment,
security, review, publication, and a complete request-to-production exercise.
Every chapter's shell example is executed by `make docs-check` as part of the
documentation build.

## Acknowledgements and Lineage

`mac`'s control-plane code is clean-room work, but the system is not built in a
vacuum. It has learned from, interoperates with, or substantially relies on the
following projects. The relationship is stated explicitly so that an
integration or protocol influence is not mistaken for copied source:

- **[Hermes Agent](https://github.com/NousResearch/hermes-agent) — vendored
  runtime:** `src/mac/_hermes` is a pruned, MAC-modified snapshot of Hermes
  Agent 0.15.1 at
  [`b1a25404b`](https://github.com/NousResearch/hermes-agent/commit/b1a25404b638bfbd79ce4d08b49afc0ee1361528).
  It supplies the agent loop, gateways, tools, plugins, and skills. The
  snapshot contract — the pin and prune policy — is
  [ADR 0001](docs/adr/0001-unify-hermes-runtime-into-mac.md); the standalone
  deploy/hermes/SNAPSHOT.md it once named was removed with the inactive
  snapshot.
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

- PostgreSQL-backed task ledger with state transitions, leases, history, evidence, dependencies, and recovery.
- Machine and agent registry with static/fungible instance kind, capabilities,
  resources, health, and availability.
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
- `persona_instance`: a running or durable persona identity such as `worker-1` (formerly `hermes_instance`).
- `platform_binding`: a Slack workspace/channel, Telegram chat, or similar binding.
- interaction task: a durable task created from a Hermes conversation with origin metadata, not copied private memory.

## Documentation

The guide is in [`docs/guide/`](docs/guide/README.md):

| | |
|---|---|
| [System Architecture](docs/guide/01-architecture.md) | what the pieces are and how work flows — mostly diagrams |
| [Getting Started](docs/guide/02-getting-started.md) | stand up a fleet, run a task, diagnose one that does not move |
| [Advanced Concepts](docs/guide/03-advanced.md) | leases, evidence, review, publication, and the known gaps |
| [The UI](docs/guide/04-ui.md) | the read-only console the hub serves at `/ui`, and the unshipped Fleet IDE prototype |
| [Developer Guide](docs/guide/05-developer-guide.md) | how to hack on mac |
| [Contributing](CONTRIBUTING.md) | filing issues and PRs that are actually tested |
| [Presentations](docs/presentation/README.md) | capabilities decks, each pinned to the commit it describes and published to Google Slides |

Those pages are written from the code and gated by
`tests/test_guide_docs_are_true.py`, which checks that every file they name
exists, every `mac` command they show resolves against the real parser, and
every edge in the task state diagram is one the control plane allows.

Every current documentation file is linked from the
[complete documentation index](docs/reference/documentation-inventory.md), and
historical material — ADRs, field notes, and design specs kept for provenance —
lives behind the visibly-labelled [historical archive](docs/archive/index.md),
which is not current product behaviour. The automated docs-graph gate
(`scripts/check-docs-graph.py`, run by `make docs-check`) traverses the internal
links out of this `README.md` and fails the build on any current doc that is
orphaned, any broken internal link, or any current doc missing from the index.

## Quick Start

```bash
# See every supported lifecycle target. Bare `make` prints the same help.
make help

# Install/link the CLI and build the hub UI.
make install

# Verify the checkout, or log in and run the hub UI against that hub.
make test
mac admin login
make run-gui
```

The hub serves its UI at `<hub>/ui` — the observability console built from
`observe/`. `make run-gui` runs that same source tree locally. The bare hub root
and the `/dashboard/*` routes require a bearer token, so an unauthenticated
browser gets `403` there; `/ui` is the front door.

`make test` runs the complete hermetic pytest suite with statement, branch, and
Python-subprocess coverage for MAC-owned `src/mac` code. Vendored Hermes
internals under `src/mac/_hermes` are excluded. Coverage is a regression safety
floor rather than a target for generating tests; see
[the test portfolio strategy](docs/testing-strategy.md). Use `make coverage`
for the same full-suite report, `make test-portfolio` to audit redundant
execution, and `make fault-replay` to prove tests detect known historical bugs.

### Linting and formatting

A single shared [Ruff](https://docs.astral.sh/ruff/) configuration lives in
`[tool.ruff]` in `pyproject.toml`, so linting and formatting are byte-identical
on every host instead of ad-hoc per-directory settings.

```bash
make lint       # check-only lint gate (scripts/run-lint.sh)
make lint-fix   # apply safe autofixes and reformat in place
```

The enforced lint set starts at the always-green correctness floor (pyflakes
logic errors and undefined names, plus syntax errors) so `make lint` is red only
for a real regression; widen `[tool.ruff.lint].select` as the codebase is
cleaned up. Ruff is a dev-only tool pinned in the `dev` extra and fetched on
demand with `uv run --with ruff`; it is not a runtime dependency. The vendored
Hermes runtime under `src/mac/_hermes` keeps its own upstream lint discipline
and is excluded.

The common lifecycle is deliberately conventional:

```bash
make install       # CLI + the hub UI bundle
make build         # Python wheel + the hub UI bundle
make clean         # generated artifacts only
make distclean     # also remove .venv and node_modules
```

Use `make install-cli` or `make install-gui` when only one surface is needed.
Installation requires Python 3.11+, Git, GitHub CLI (`gh`), npm, and CodeGraph;
build and test targets also require `uv`.
Every source-consuming build, install, run, and test target refreshes CodeGraph
first; the installed pre-push hook does the same. Fleet configuration/deployment
is intentionally separate under `make setup` and `make deploy`.

For local control-plane/API development after installation:

```bash

# Required: a 32+ char secret used to derive the Fernet key for the secrets table.
# Without it, the CLI and API both refuse to start.
export MAC_SECRET_KEY="$(openssl rand -base64 32)"

# PostgreSQL is the only supported backend. scripts/start-test-postgres.sh
# finds a local server or starts a container and prints the DSN to export.
eval "$(scripts/start-test-postgres.sh)"
export MAC_DB="$MAC_TEST_PG_URL"

uv run --extra postgres mac --db "$MAC_DB" admin init
uv run --extra postgres uvicorn mac.api:app --reload
uv run mac-hermes --url http://127.0.0.1:8789 --help
```

For standalone local work, pass `--db <postgres-dsn>`. `MAC_DB` is server
configuration; it does not implicitly opt CLI commands into direct database
access. Without `--db`, the CLI selects a configured hub (`--hub-url`,
`MAC_API_URL`, `MAC_URL`, `MAC_HUB_URL`, or `~/.mac/fleets.yaml`) and otherwise
refuses to run rather than guessing an authority. On a deployed hub, where
`MAC_DB` and `MAC_HUB_URL` are both present, operator commands use the HTTP API.

Control-plane server startup likewise requires an explicit `MAC_DB` /
`MAC_DATABASE_URL` PostgreSQL DSN.

### Control-plane authority is not repository-local task storage

`--db` selects a **direct PostgreSQL authority**. It is appropriate for a
standalone development database, tests, explicit migrations, and stopped-hub
maintenance; it is not an offline cache, and its tasks are never uploaded,
merged, or reconciled with a remote hub. Aiming `--db` at a deployed hub's
configured authority therefore requires `--local-authority`. MAC also probes
the configured local health endpoint and refuses that maintenance mode while
the hub is running.

Normal operations against a running hub omit both flags:

```bash
mac task stats
mac task show task_...
```

For exceptional direct-database maintenance, stop `mac-api` first, preserve its
existing `MAC_DB` environment, and make the override explicit:

```bash
mac --local-authority task stats
```

Restart the hub before resuming fleet work. Opening an existing database through
this direct path does not run schema DDL. Schema creation and additive migrations
run at control-plane startup or through an explicit `mac --db <dsn> admin init`.

The hub ledger is intentionally not rooted in one checkout. It coordinates
leases, agents, reviews, workflows, evidence, A2A work, non-repository
operations, and tasks spanning multiple repositories. A task reaches source
through its project repository registration and execution contract.

Fleet deployment assigns `MAC_CONTROL_PLANE_ROLE=hub` only to the selected hub.
That host receives the explicit database configuration and runs `mac-api`.
Spokes have `MAC_CONTROL_PLANE_ROLE=client`, no `MAC_DB`, and register their
workers and Hermes identities through the hub API.

GitHub issues remain external project-planning records. `.tickets/` is ignored
local migration/compatibility state, not another execution authority. Bridges
may create a hub task from an external issue, but the resulting `mac task`
record in the selected control-plane authority is what agents claim and run.

One-time imports into a hub authority are available through
`mac admin migrate import`, which replays a JSONL record stream. Run
`mac admin migrate help` for the current set.

### Client bootstrap status

`mac admin login` bootstraps a new client from verified SSH access to the hub. It
pins the host identity, opens the hub-local API tunnel, requests an independently
revocable scoped credential over SSH, validates that credential through the
tunnel, and only then atomically installs the local profile and mode-`0600`
secret. Managed tunnels reconnect on the next profile-backed command if their
SSH process has exited.

```bash
mac admin login --ssh mac@hub.internal \
  --identity-file ~/.ssh/mac-production \
  --known-hosts-file ~/.ssh/mac-production-known-hosts \
  --fleet production --profile production --client-id my-laptop

mac admin login status --profile production
mac task stats
mac agent list
mac admin login renew --profile production
mac admin logout --profile production --revoke

# Directly reachable scoped endpoint (automation/operator provisioning):
export MAC_API_URL=https://mac.example.internal
export MAC_API_TOKEN=<scoped-client-token>
mac admin diagnostics
mac task stats

# Shared-admin recovery for an existing operator workstation only:
mac admin fleet sync-token --fleet my-fleet
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

# Immediate operator stop: revoke the live lease and make the owning loop
# worker terminate its active executor tree on the next cancellation poll.
mac task cancel task_running --reason "operator stopped obsolete work"

git fetch --prune origin
mac --json admin repo refs audit --repo .
mac --json admin repo refs prune --repo .          # dry-run
mac --json admin repo refs prune --repo . --execute --actor operator

# Hub-wide automatic reconciler status and immediate admin-triggered passes.
mac --json admin repo refs status
mac --json admin repo refs reconcile --mode audit --actor operator
mac --json admin repo refs reconcile --mode prune --actor operator
```

See [Managed Repository Ref Hygiene](docs/repository-ref-hygiene.md) for the
schedule/configuration, disposition policy, exact-SHA deletion contract, grace
periods, monitoring, and recovery.

## Stand Up Your Own Fleet

The Quick Start above installs the CLI from a checkout. This creates a *fleet*:
a named hub plus the workers that join it. A fleet is a first-class object —
`~/.mac/fleets.yaml` holds as many as you like, each with its own hub URL and
token, selected with `--fleet <name>` or `$MAC_FLEET`.

**Before you start**, have SSH key access working to every host you intend to
use, from the machine you are running the wizard on. The wizard configures; it
does not fix SSH. You also need at least one upstream LLM provider API key
(nvidia / openai / anthropic / perplexity) — the wizard will not finish without
one, because a fleet with no provider cannot execute a task.

### 1. Create the fleet and its hub

Run the wizard on the machine that will be the hub, or point it at one:

```bash
bash setup.sh
```

It asks two questions before anything else — whether you are on the machine
being configured, and whether this is a **hub** or a **worker**. Choose `hub`.
It then collects the fleet name, supervisor (`auto` picks launchd on macOS,
systemd on Linux), network provider (Tailscale by default), and your provider
key, writes `~/.mac/fleets.yaml` and `~/.mac/.env`, and deploys.

To write the config without deploying yet:

```bash
bash setup.sh --configure-only
```

Neither file is in this repository, and neither should ever be committed:
fleet topology and provider keys are yours, not the product's.

### 2. Add workers

Run the wizard again for each additional host and choose `worker`. It looks up
the existing fleet by hub name and asks only what is new — the worker's name,
SSH target, OS, supervisor, and mode:

```bash
bash setup.sh
```

Workers do not need a checkout of this repository. Deploy ships the source to
each host and installs it.

### 3. Watch it work

```bash
mac --fleet <name> agent list       # who joined, and what hardware they have
mac --fleet <name> task ready       # what the fleet could pick up right now
```

Open the console in a browser at `http://<hub>:8789/ui`. It is read-only, and
shows tasks and agents actually moving through states rather than a snapshot of
counts.

### 4. Drive it from a coding CLI

```bash
mac --fleet <name> task create "the thing you want done" \
    --description-file=brief.txt
```

The fleet claims it, works it in a sandbox, and publishes through review. Watch
the transitions land in the console's live view while you talk to Claude Code,
Codex or Cursor in the other window.

`mac admin mcp serve` exposes the ledger to a coding agent as MCP tools, so the
agent can read and file tasks directly instead of shelling out and parsing
tables.

### If something does not come up

```bash
mac --fleet <name> admin fleet doctor      # what the hub thinks is wrong
mac --fleet <name> task why-unclaimed <id> # why a specific task is not moving
mac --fleet <name> task preflight ...      # before filing: could this ever be claimed?
```

The most common cause of a task that is created and never claimed is asking for
a *capability* the fleet does not advertise. Host facts like `linux` are not
capabilities — see [Requirements: capabilities versus
hardware](skills/mac-cli/SKILL.md).

## API

Run the REST API with `MAC_SECRET_KEY` and an explicit database set:

```bash
MAC_SECRET_KEY="..." MAC_DB="postgresql://..." uv run uvicorn mac.api:app --reload
# or use factory mode to be explicit:
MAC_SECRET_KEY="..." MAC_DATABASE_URL="postgresql://..." \
  uv run uvicorn mac.api:create_app --factory --reload
```

Set `MAC_API_TOKEN` for one admin token, or `MAC_API_TOKENS` as JSON such as
`{"reader":["read"],"worker":["agent","dispatch"]}` to require scoped bearer
tokens. Hub-local `mac admin client enroll` writes independently revocable hashed
principals to `MAC_CLIENT_PRINCIPALS_FILE`; the API hot-reloads that registry.
With no static token or enrolled client configured, the local prototype API
remains open for development.

The observability console is served at `/ui` (and `/ui/console`). Static assets
are public so the browser can load the shell, while data requests use the same
API token rules as the REST API.

It is READ-ONLY, and that is the point rather than a limitation. It answers
what the fleet is doing — tasks and agents moving through states — with views
for live movement, stuck work, agents, projects, pipelines, dream & nap cycles,
telemetry, the merge queue, and a per-task drill-down. The live view charts
state transitions per time bucket, because a count tells you 360 tasks are
blocked and only a series tells you whether they are arriving or draining.

Two properties it will not trade away:

- A section the hub could not read is ABSENT and named in `degraded`, never
  rendered as a plausible zero. `observe/tests/readonly.test.ts` asserts the
  whole app is read-only.
- The command-and-control dashboard it replaced was retired deliberately. That
  shell called `/dispatch/tick`, `/agents/bulk`, `/roles/seed`,
  `/notifier/deliver` and `/secrets`; a UI whose job is to observe cannot also
  be the one that commands.

The console is built from `observe/` (React + Vite) into `src/mac/ui/console/`,
which is committed, so serving it needs no Node.js at runtime. Rebuild with
`npm --prefix observe run build` after changing the source; CI fails if the
committed bundle drifts from it.

Mutating operations live in the CLI (`mac ...`), the REST API, and the Fleet
IDE (`ide/`).

Key route groups:

- `/tenants`, `/users`, `/personas`
- `/persona-instances`, `/persona-instances/{id}/context`, `/persona-instances/{id}/work-context`, `/platform-bindings`
- `/dashboard/state`, `/dashboard/stream`, `/dashboard/observe`, `/dashboard/observe/tasks/{id}`, `/dashboard/workflow-plan/preview`, `/dashboard/workflow-plan/accept`
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

## The hub UI

The hub serves one browser UI: the read-only observability console, at
`<hub>/ui`. Its source is `observe/`; `npm run build` there writes the committed
bundle in `src/mac/ui/console/`, which is what the hub's `/ui/assets` mount
serves. From the repository root:

```bash
make install-gui        # build the bundle the hub serves
mac admin login
make run-gui            # run that same tree locally on http://127.0.0.1:5274
make build-gui
make package-gui        # writes dist/mac-hub-ui.tar.gz
```

`make run-gui` proxies `/dashboard/*` to `$MAC_API_URL` (default
`http://127.0.0.1:8789`) and sends the bearer from `MAC_UI_PROXY_TOKEN`,
`MAC_DEPLOY_HUB_TOKEN` or `MAC_API_TOKEN` — sourced from `~/.mac/.env` if
present, never printed. `tests/ui/test_hub_ui_is_one_tree.py` asserts that what
`run-gui` runs and what the hub serves are the same tree.

### The Fleet IDE prototype (`ide/`)

The React + Monaco fleet IDE in `ide/` is a **local prototype that no hub
serves**, kept runnable behind its own targets. `make install`, `make build` and
`make package` do not produce it, and no deploy step installs it. See
[ADR 0025](docs/adr/0025-the-hub-ui-is-the-observability-console.md).

```bash
make ide-install
mac admin login
make ide-run            # Vite on http://127.0.0.1:5273
make ide-build
make ide-package        # writes dist/mac-ide-web.tar.gz
```

For local auth, `make ide-run` first reuses the active scoped client profile
created by `mac admin login`, including its managed SSH tunnel. In an interactive
terminal it then prompts for the target hub URL; press Enter for the profile
endpoint, enter another `http://` or `https://` host, or set `IDE_API_URL`
beforehand to skip the prompt. The credential stays inside the local Vite proxy
and is not exposed to browser storage. If no active profile exists, the launcher
can read a deploy-created owner-only handoff file:

```bash
IDE_HANDOFF_FILE="$HOME/.mac/fleet-ide-handoff.json" IDE_OPEN=1 make ide-run
```

The handoff file keeps the bearer out of browser-visible environment, argv,
stdout, and deploy logs. As a final compatibility fallback the launcher sources
`~/.mac/.env` and selects a token without printing it. Set `IDE_FLEET=<fleet>` to
prefer the matching `MAC_API_TOKEN__<FLEET>` key; otherwise the launcher falls
back to `MAC_DEPLOY_HUB_TOKEN` and then `MAC_API_TOKEN`. The Electron dashboard
wrapper in `desktop/` renders this prototype and is maintenance-only.

## Tell Agents To Work On A Project

Agents work on dispatchable tasks, not on repositories by implication. The
operator flow is below. Use `--db` for local mode, or omit it when your CLI is
already pointed at a hub through `--hub-url`, environment, or `~/.mac/fleets.yaml`.

```bash
mac --db "$MAC_DB" project register git@github.com:ORG/REPO.git#main --project my-project
# After the onboarding task has produced .mac/project.yaml and that contract
# exists in a hub-visible checkout:
mac --db "$MAC_DB" bridge repository register my-project /srv/repos/my-project --project my-project
# or, for a non-repository/manual project:
mac --db "$MAC_DB" project create my-project --active

mac --db "$MAC_DB" task create "Fix failing tests" \
  --project my-project \
  --description-file desc.txt \
  --required-capabilities python

mac --db "$MAC_DB" project activate my-project      # if the project was staged/paused
mac --db "$MAC_DB" task release task_...            # if the task was created with --no-dispatch
mac --db "$MAC_DB" dispatch tick --limit 10         # assign ready work now
```

The canonical project registration is `GIT_URL#BRANCH`; omitting the fragment
means `#main`. The same Git URL can therefore have independent MAC projects for
different branches, while registering the same URL and branch twice is rejected.
When `--project` is omitted, a non-main branch is named `REPO@BRANCH`.

Project records have a complete operator surface:

```bash
mac project list
mac project show my-project
mac project update my-project --branch release/next
mac project update my-project --registration git@github.com:ORG/REPO.git#other
mac project unregister my-project --force
```

`unregister` refuses projects with linked tasks or internal checkout attachments unless `--force` is
given; forced removal detaches historical tasks and disables linked checkout
attachments rather than deleting their history.

Loop-mode agents claim matching work from `mac task ready`. For explicit manual
assignment, use `mac task claim <task_id> <agent_id>` followed by
`mac task start <task_id> <agent_id>` with the same transport flags.

## Current Task Workflow

For repository-backed work, the production path is:

1. A project's git repository is registered — run `mac project register` from
   a checkout, pass another local checkout path, or use `mac project register
   <git-url>[#branch]` for remote-first onboarding. Registration creates the
   contract-authoring onboarding task. Follow it with `mac bridge repository
   register <name> <path> --project <project>` once `.mac/project.yaml` exists
   in a hub-visible checkout. The mac task ledger is canonical; ready work is
   `mac task ready`.
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
mac --db "$MAC_DB" machine register workstation-1
mac --db "$MAC_DB" agent register machine_... worker --capabilities python,review
mac --db "$MAC_DB" tenant register personal
mac --db "$MAC_DB" persona register tenant_... AssistantOne --soul-ref hermes://personal/assistant-one/SOUL.md --memory-scope hermes://personal/assistant-one/memory
mac --db "$MAC_DB" hermes register tenant_... assistant-one --persona-id persona_... --home-ref hermes://personal/assistant-one
mac --db "$MAC_DB" binding register tenant_... hermes_... slack T123/C456 --display-name "#ops"
mac --db "$MAC_DB" interaction task hermes_... "Investigate deployment failure" --platform-binding-id binding_...
mac --db "$MAC_DB" task create "Implement feature" --required-capabilities python
mac --db "$MAC_DB" dispatch tick
mac --db "$MAC_DB" task show task_...

# Inspect secret-free repository-access outcomes used by reviewer routing.
mac --db "$MAC_DB" --json memory search \
    --record-type fleet_learning:repository_access --order desc --limit 50

# Secrets: prefer stdin or file input over argv to keep values out of shell history.
echo -n "$GH_TOKEN" | mac --db "$MAC_DB" secret set github-token \
    --from-stdin --scopes '{"capabilities":["deploy"]}' --created-by human
mac --db "$MAC_DB" secret set release-key --from-file ./release.key \
    --scopes '{"capabilities":["deploy"]}' --created-by human

# Rollouts require a pinned runtime and verified sha256 artifact before install.
mac --db "$MAC_DB" runtime create mac-runtime \
    --manifest '{"image":"python:3.12@sha256:abc123","dependencies":["fastapi==0.111.0"]}' \
    --created-by human
mac --db "$MAC_DB" rollout create 1.2.0 canary --runtime runtime_... \
    --artifact-uri artifact://mac/1.2.0 --artifact-hash sha256:abc123 \
    --health-policy '{"required_checks":["runtime","canary"]}' \
    --created-by human
mac --db "$MAC_DB" rollout advance rollout_... start_canary --actor human
mac --db "$MAC_DB" rollout health rollout_... \
    --checks '{"runtime":"healthy","canary":"ok"}' --actor monitor

# Evaluation: define a scored eval set, record runs against rollout versions,
# and gate promotion on a passing run.
mac --db "$MAC_DB" eval set create task-success-rate \
    --scoring higher_is_better --baseline-score 0.90 --regression-threshold 0.02
mac --db "$MAC_DB" eval run record evalset_... rollout_version 1.2.0 0.93
mac --db "$MAC_DB" rollout create 1.3.0 canary --runtime runtime_... \
    --artifact-uri artifact://mac/1.3.0 --artifact-hash sha256:def456 \
    --required-eval-set-id evalset_... --created-by human
mac --db "$MAC_DB" rollout advance rollout_... start_canary --actor human
# promote refused until a passing eval run exists for version 1.3.0
mac --db "$MAC_DB" rollout advance rollout_... promote --actor human

# Unified audit stream: one query across task/agent/project/fleet/rollout/eval_set/secret/environment events.
mac --db "$MAC_DB" events list --limit 50
mac --db "$MAC_DB" events list --subject-type rollout --subject-id rollout_...
mac --db "$MAC_DB" events list --prefix rollout. --since 2026-05-17T00:00:00+00:00
mac --db "$MAC_DB" events list --actor monitor --event-type rollout.health_failure_during_rescue
mac --db "$MAC_DB" observability list --layer control_plane --subject-type fleet

# Artifact registry + environment deployments + fleet build inventory.
mac --db "$MAC_DB" artifact register image sha256:abc... artifact://mac/v1.2.0 \
    --created-by ci --sbom-uri sbom://mac/v1.2.0.spdx --signers ci,release-manager
mac --db "$MAC_DB" env register staging --channel release --created-by human
mac --db "$MAC_DB" env deploy staging sha256:abc... --actor release-bot
mac --db "$MAC_DB" env current staging
mac --db "$MAC_DB" agent heartbeat agent_... --running-digest <runtime-digest>
mac --db "$MAC_DB" fleet build-distribution

# Minimal worker harness: register/heartbeat first without claiming, then run
# an executor-backed claim/start/evidence/submit loop.
mac-agent --url http://hub.example.internal:8789 --register --agent-name worker-1 \
    --hostname worker-1.local --capabilities python,ops,review \
    --resources '{"capacity":2}' --heartbeat-only
mac-agent --url http://hub.example.internal:8789 --register --agent-name worker-1 \
    --capabilities python,ops,review --allowed-projects mac-canary --claim-only-canary-tasks \
    --dry-run-claim
mac-agent --url http://hub.example.internal:8789 --agent-id agent_... \
    --workspace ~/.mac-agent/workspaces --allowed-projects mac-canary \
    --claim-only-canary-tasks --executor ~/.mac/bin/mac-hermes-task-executor
mac-agent --url http://hub.example.internal:8789 --register --agent-name worker-1 \
    --capabilities python,ops,review --loop --workspace ~/.mac-agent/workspaces \
    --allowed-projects mac-canary --claim-only-canary-tasks \
    --executor ~/.mac/bin/mac-hermes-task-executor

# Typed AgentBus: durable ordered content chunks; this is transport, not exec.
mac --db "$MAC_DB" agentbus publish agent_sender --recipient-agent-id agent_recipient \
    --content-type application/vnd.mac.delta+json \
    --payload '{"kind":"delta","content":"hello"}'
mac --db "$MAC_DB" agentbus read bus_... agent_recipient
```

Hermes-facing API adapter:

```bash
mac-hermes --url http://127.0.0.1:8789 register \
  --tenant personal \
  --persona AssistantOne \
  --instance assistant-one \
  --soul-ref hermes://personal/assistant-one/SOUL.md \
  --memory-scope hermes://personal/assistant-one/memory \
  --binding slack:T123/C456:#ops

mac-hermes --url http://127.0.0.1:8789 task hermes_... \
  "Investigate deployment failure" \
  --summary "The Slack deployment thread reports a failed publish step." \
  --platform-binding-id binding_... \
  --conversation-ref slack://T123/C456/1712345678.000100 \
  --required-capabilities ops

mac-hermes --url http://127.0.0.1:8789 reply task_...
mac-hermes --url http://127.0.0.1:8789 writeback hermes_... task_...
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
- [Fleet Node Onboarding Checklist](docs/fleet-node-onboarding-checklist.md)
- [SSH Client Bootstrap Contracts](docs/client-bootstrap-contract.md)
- [Repository Runtime Contract](docs/repository-runtime-contract.md)
- [Managed Repository Ref Hygiene](docs/repository-ref-hygiene.md)
- [Fleet Operational Learning](docs/fleet-operational-learning.md)
- [OpenClaw public identities](docs/openclaw-identities.md)
- [Review-strategy experiments](docs/review-strategy-experiments.md)
- [Integration Authority Contract](docs/integration-authority-contract.md)
- [Soul Preservation Runbook](docs/soul-preservation-runbook.md)
- [Scaling Plan](docs/archive/field-notes/scaling-plan.md) (historical)
