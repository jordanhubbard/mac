# Source notes — AgentFabric overview

This is the factual ledger for both members of this package. Every claim in the deck and
the narrative must appear here with an authority inside the control-plane tree, and every
counted figure must carry the command that produced it and the date it was measured.

If a claim is not in this file, it does not belong in the deck.

**Audited tree:** `jordanhubbard/mac` at `2976182`, measured 2026-08-30.

## Identity and framing

| Claim | Authority | Status |
| --- | --- | --- |
| AgentFabric is a multi-agent coordinator control plane that owns durable operational truth; human-facing runtimes own conversation, personality, memory, and messaging gateways | `README.md`, `docs/authority-boundary.md` | implemented |
| The control plane owns tasks, leases, routing, reviews, evidence, secret handles, runtime manifests, rollout state, and audit trails | `README.md`, `src/mac/models.py`, `src/mac/task_lifecycle.py` | implemented |
| The gateway (conversational runtime) holds no operational authority | `docs/hermes-boundary.md`, `docs/authority-boundary.md` | implemented |

"AgentFabric" is the presentation name for this control plane; the repository, CLI, and
modules use `mac`. The deck never claims the two are different systems.

## NVIDIA technology re-use (slide 7)

| Project | Role in AgentFabric | Authority |
| --- | --- | --- |
| NVIDIA OpenShell | execution security: Landlock filesystem policy, seccomp syscall filter, deny-by-default L7 egress, sandbox lifecycle, normalized action events. One guardrail authority, not two competing ones. | `src/mac/openshell_service.py`, `src/mac/executor_sandbox.py`, `src/mac/sandbox_egress.py`, `src/mac/openshell_collector.py`, `docs/openshell-sandbox.md`, ADR 0008 (Accepted) |
| NVIDIA NeMo Relay | optional observability: request, task, tool, and model activity mapped into Relay scopes, enabled through the `relay` packaging extra (`nemo-relay==0.3.0`) | `src/mac/relay_observability.py`, `pyproject.toml` (`[project.optional-dependencies] relay`), `docs/openshell-nemo-relay-integration.md`, `docs/openshell-nemo-relay-e2e.md` |
| NVIDIA HGX | bounded elastic provider-session capacity; onboarding is an explicit operator action with a durable receipt | `docs/hgx-elastic-capacity.md`, `AGENTS.md` (`hgx list` / `hgx ssh` transport), ADR 0005 (Proposed — the elastic tier beyond the operator-driven path is a proposal) |
| NVIDIA NemoClaw | compatibility and design reference for the conversational agent boundary; **not** the deployed gateway implementation | `README.md`, `docs/hermes-boundary.md` |

The deck must state the HGX and NemoClaw qualifications. Presenting NemoClaw as the
deployed gateway, or unbounded elastic capacity as shipped, is a factual error.

## Open-source re-use (slide 8)

| Group | Projects | Authority |
| --- | --- | --- |
| State | PostgreSQL (fleet authority), SQLite (local development), versioned migrations | `CLAUDE.md` ("the test suite runs against PostgreSQL, not SQLite, because that is what the fleet runs"), `src/mac/schema_migrations.py`, ADR 0021 (Proposed — the migration *policy* is proposed; versioned migrations exist) |
| Service | FastAPI, Uvicorn, Pydantic, httpx | `pyproject.toml`, `src/mac/api.py`, `src/mac/http_routes/` |
| Execution | Docker Engine / Moby, Kubernetes, OpenClaw, OpenAI Codex CLI, OpenCode | ADR 0008 (Accepted), `src/mac/k8s/runner.py`, `src/mac/coding_agent.py`, `docs/openclaw-identities.md` |
| Protocol | ACP, A2A agent cards, MCP, OCSF event streams | `src/mac/acp/protocol.py`, `src/mac/a2a/card.py`, `src/mac/mcp_server.py`, `src/mac/openshell_collector.py`, ADR 0006 (Proposed — ACP *support scope*) |

Every entry is a dependency, not a fork. The MCP server and the Python client are clients
of the same HTTP surface rather than parallel implementations (`src/mac/mcp_server.py`).

## Mechanisms AgentFabric adds (slide 9)

| Mechanism | Authority | Status |
| --- | --- | --- |
| Durable task ledger: a task is a state machine with an owner, a lease, dependencies, and history | `src/mac/models.py` (`TaskState`), `src/mac/task_lifecycle.py`, `docs/task-dependency-semantics.md`, ADR 0004 (Accepted) | implemented |
| Named gate decisions rather than booleans, so refusals are explainable | ADR 0022 (Proposed as a general contract), `docs/adr/0011-hub-review-verification-scope.md` (Accepted) for the review gate | review gate implemented; the general contract is a proposal |
| Ordered, capability-filtered coding-route ladder | `src/mac/coding_agent.py` (`AGENT_PRIORITY`), `docs/coding-route-ladder.md` | implemented |
| Evidence closure: build, test, review, and publication artifacts kept as pointers with the task | `AGENTS.md` (`mac.worker_evidence.v1` manifest), `src/mac/observability_service.py` | implemented |
| Break-glass: recovery is a granted, listable, revocable, reason-bearing authorization | `mac task break-glass` / `break-glass-list` / `break-glass-revoke`, `docs/break-glass-host-recovery.md` | implemented |

The route ladder order is `("opencode", "pi", "claude", "codex", "cursor")` —
`src/mac/coding_agent.py:111`. opencode is first for credential durability, not model
quality; the module comment states this. Do not present the order as a quality ranking.

## Containment (slide 10)

| Claim | Authority |
| --- | --- |
| Agent process tree is owned and reaped; syscall filter applied; never runs as root | `src/mac/executor_sandbox.py`, `src/mac/worker.py`, `docs/openshell-sandbox.md` |
| Read-only and read-write paths are allow-listed via Landlock | `src/mac/openshell_service.py`, `src/mac/executor_sandbox.py` |
| Egress is deny-by-default with declared hosts per project and per task | `src/mac/sandbox_egress.py` |
| Secrets are handles resolved at use and never printed | `docs/secrets-management-guide.md` |
| An agent never accepts its own work | `docs/adr/0011-hub-review-verification-scope.md`, `docs/authority-boundary.md` |

"Never self-approves" is an authority boundary enforced by the control plane, not a
sandbox control. The deck says so.

## Actors, lifecycle, coordination, fleet (slides 12–15)

| Claim | Authority |
| --- | --- |
| Eight actors, one authority each: requester, gateway agent, hub, dispatcher, worker, coding executor, reviewer agent, operator | `docs/authority-boundary.md`, `src/mac/services.py`, `src/mac/worker.py` |
| 12 task states | `src/mac/models.py` (`TaskState`): open, waiting, blocked, claimed, running, needs_review, needs_input, stopped, reviewing, completed, failed, cancelled |
| Terminal states are completed, failed, cancelled | `src/mac/models.py` (`TERMINAL_TASK_STATES`) |
| A hold is a flag (`metadata.no_dispatch=true`), not a state; `mac task release` removes the key | `AGENTS.md`, `src/mac/task_lifecycle.py` |
| Leases are time-bounded and recoverable; recovery verbs exist for stranded and stalled work | `src/mac/task_lifecycle.py`, `AGENTS.md` |
| AgentBus is a broadcast medium with lifecycle verbs (stand down, abort, pause, resume, status); the ledger, not the bus, is the system of record | `src/mac/agentbus_service.py`, `src/mac/agentbus_control.py`, `src/mac/agentbus_broadcast.py`, ADR 0026 (Proposed — *first-class operations emit bus events*) |
| Operational learning is recorded as secret-free memories that bias future routing | `AGENTS.md` ("Fleet Operational Learning"), `docs/fleet-operational-learning.md` |
| macOS nodes are host installs by decision | ADR 0015 (Accepted), `docs/adr/0015-macos-nodes-are-host-installs.md` |
| Docker Engine / Moby is the only container runtime | ADR 0008 (Accepted) |
| Kubernetes dispatch folds claim, launch, and stuck-Job reconciliation into one orchestrator | `src/mac/k8s/runner.py` |
| HGX sessions are onboarded by explicit operator action with a durable receipt | `docs/hgx-elastic-capacity.md`, `AGENTS.md` |
| Fleet targets resolve from `~/.mac/fleets.yaml`, which is the source of truth | `AGENTS.md` ("Fleet Host Resolution") |

## Models and money (slide 16)

| Claim | Authority | Status |
| --- | --- | --- |
| One route event per model call, carrying input, output, and streaming state, attributed to task, project, or agent | `src/mac/observability_service.py` (`llm.route` events), ADR 0017 | implemented |
| Cost is priced at read time against a models catalog, so a price-table change re-values history | `estimate_route_cost()` in `src/mac/scientific_optimizer.py`, `src/mac/models_catalog.py` | implemented |
| Metering **enforced** at the router rather than trusted from the caller | ADR 0017 (**Proposed**) | proposed |
| Coverage gaps are measured, not averaged away | ADR 0017 quantifies them: over the seven days to 2026-08-19, on 28,352 `llm.route` events, 8,352 routes (29.5%) recorded `input_tokens = null`, 5,948 were flagged `stream_no_usage`, and 2,474 routes were attributed to nothing | measured, dated |

Any spend figure quoted from this package must carry its measurement date. The deck must
not say "every model call is metered at the router" — today the router captures what the
caller reported, and the gap is quantified above.

## Measured surface area (slide 18)

Counts describe surface area, not maturity. Re-measure on every regeneration; do not carry
these forward.

| Figure | Value | Command (repository root, `2976182`, 2026-08-30) |
| --- | --- | --- |
| control-plane modules | 221 | `ls src/mac/*.py \| wc -l` |
| HTTP route declarations | 435 | `grep -rhoE "@(app\|router)\.(get\|post\|put\|patch\|delete)\(" src/mac \| wc -l` |
| CLI leaf commands | 458 | walk `mac.cli.build_parser()` and count parsers with no subparser map |
| top-level CLI groups | 4 objects (`project`, `task`, `agent`, `admin`) plus `help` | `mac.cli.build_parser()` subparser map |
| task states | 12 | `TaskState` in `src/mac/models.py` |
| coding routes on the ladder | 5 | `AGENT_PRIORITY` in `src/mac/coding_agent.py` |
| dispatch targets | 2 (fleet nodes, Kubernetes) | `src/mac/k8s/runner.py` plus the fleet dispatcher path |

## Implemented / decided / proposed (slide 19)

Placement is taken from each ADR's own status line.

| Column | Item | Authority |
| --- | --- | --- |
| Implemented | durable ledger, leases, recovery | ADR 0004 (Accepted), `src/mac/task_lifecycle.py` |
| Implemented | sandboxed execution and egress policy | ADR 0008 (Accepted), `src/mac/sandbox_egress.py` |
| Implemented | independent review gates | ADR 0011 (Accepted) |
| Implemented | route events, priced at read time | `src/mac/observability_service.py`, `estimate_route_cost()` |
| Decided, not yet runtime | agent-initiated review scope | ADR 0016 (Accepted 2026-08-20) |
| Decided, not yet runtime | native steward plus containerized execution on Linux | ADR 0012 (**Accepted; implementation deferred pending fleet measurement**) |
| Proposed | metering enforced at the router | ADR 0017 (Proposed) |
| Proposed | route-search-path contract | ADR 0029 (Proposed) |
| Proposed | task view as a graph | ADR 0018 (Proposed) |
| Proposed | retrieval and extraction pipeline | ADR 0030 (Proposed) |

## Prohibited claims

- No ROI, productivity, or throughput percentages. None are measured, so none may appear.
- The forty-agent failure field on slide 3 is illustrative of failure classes, not a
  measured failure rate.
- Evidence indexes what happened; it does not authorize a release. Acceptance and
  publication remain explicit decisions by an authorized actor.
- The AgentBus is not the system of record.
- No claim that the full fleet matrix has been qualified; node classes are configuration
  evidence, not qualification evidence.
