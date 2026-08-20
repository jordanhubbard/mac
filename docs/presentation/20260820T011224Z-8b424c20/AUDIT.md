# Audit — every claim in this deck, traced to source

Audited at commit `8b424c20` on 2026-08-20T01:12:24Z, by reading the tree directly. Generated
references (`docs/reference/cli.md`, `docs/reference/openapi.md`) are treated as authoritative for
surface counts because CI fails when they drift from the live parser and OpenAPI schema.

**Where the README and the code disagree, this audit follows the code**, and says so.

---

## 1. Counts

| Claim | How it was obtained |
|---|---|
| 195,764 lines of Python under `src/` | `find src -name "*.py" \| xargs wc -l` |
| 201 modules in `src/mac` | `ls src/mac/*.py \| wc -l` |
| 470 test files | `ls tests/*.py \| wc -l` |
| 408 HTTP routes | `grep -cE "^\| \`(GET\|POST\|PUT\|PATCH\|DELETE)\`" docs/reference/openapi.md` |
| 123 CLI verbs | Parsed from `docs/reference/cli.md`: task 44, admin 53, agent 17, project 9 |
| 18 book chapters | `nav:` in `mkdocs.yml`, `book/01`–`book/18` |
| 18 ADRs | `docs/adr/0001`–`0018` |
| 5 coding-agent routes | `src/mac/coding_agent.py` — `claude`, `codex`, `cursor`, `opencode`, `pi` |

## 2. The object model (diagram 01)

| Claim | Source |
|---|---|
| The CLI is organised around project / task / agent / admin | `docs/reference/cli.md`, `mac --help`: "The objects mac models. Start here: project … task … agent" |
| project = "a unit of work ownership: repositories, policy, dispatch state" | `mac --help` verbatim |
| task = "one unit of work: the thing agents claim, run, and publish" | `mac --help` verbatim |
| agent = "a worker that claims and executes tasks on a machine" | `mac --help` verbatim |
| Project dispatch pause is separate from per-task staging | `README.md` Core Contracts: "project-level dispatch pause is a separate gate" |
| Recovery verbs | `mac task --help`: `recover-stranded`, `recover-finalizer`, `recover-stalled-finalizer` |
| Break-glass is grantable/listable/revocable | `mac task --help`: `break-glass`, `break-glass-list`, `break-glass-revoke` |
| Visibility is not a dispatch gate | `docs/adr/0014-visibility-is-not-a-dispatch-gate.md`; commit `6526dd65` "Stop treating agent visibility as a dispatch gate" |
| Normalized effective allocation in the hardware snapshot | commit `c6766a14` |
| Repository runtime contract before bootstrap | `README.md` Core Contracts; `docs/repository-runtime-contract.md` |
| Generated CLI reference is checked, not hand-written | `scripts/generate-docs-reference.py`; `Makefile` target `docs-build` runs it with `--check` |
| MCP server is a client, not a second implementation | commit `efb428e9`: "Every tool goes through RemoteDispatch… A test asserts the module contains no urllib/requests/HubClient" |
| Python client is contract-checked against the hub | commit `58d71fc8`; `tests/test_dispatch_route_contract.py` referenced by `efb428e9` |
| Client warns when older than the hub | commit `1e747d35` |
| ACP and A2A are implemented specifications | `README.md` lineage section; routes `/.well-known/acp`, `/.well-known/agent-card.json`, `/a2a` in `docs/reference/openapi.md` |
| `/ui` is read-only observability; the C2 dashboard was retired | commits `d725fa12`, `43887f68`, `27ed8af9` |
| Scoped bearer tokens | `README.md`: "Optional scoped API bearer tokens for read/write/agent/dispatch/secret/admin access" |

## 3. Task lifecycle (diagram 02)

| Claim | Source |
|---|---|
| Eleven states | `src/mac/models.py`, `class TaskState`: open, waiting, blocked, claimed, running, needs_review, needs_input, reviewing, completed, failed, cancelled |
| Three terminal states | `src/mac/models.py`, `TERMINAL_TASK_STATES` = completed, failed, cancelled |
| Non-terminal work is the default list view | `src/mac/models.py` comment: "Every state that is NOT terminal: work that still wants something from somebody. This is the default view for `mac task list`"; commit `975827d9` |
| Hub drives RUNNING → NEEDS_REVIEW → REVIEWING → COMPLETED | `docs/adr/0016-agent-initiated-review.md`, Context |
| Leases and fences authorize mutations | `README.md` Core Contracts; ADR 0016 "What must not be lost — Liveness" |
| Dispatcher accounts for pool policy, resources, capacity, stale heartbeats, expired leases | `README.md` Core Contracts |
| Approval binds to current executor evidence for that exact attempt | `README.md` Core Contracts; `docs/review-strategy-experiments.md` |
| Reviewer must differ by agent and by model | `docs/review-strategy-experiments.md` |
| Blind arm physically renames `executor-evidence.json` out of the workspace | `docs/review-strategy-experiments.md`, "Evidence-withheld discovery" |
| Reviews record what was said, not only the vote | commit `7927dc16` |
| CodeGraph source-change audit is mandatory; affected-test selection | `README.md` lineage: "repository analysis, affected-test selection, and the mandatory source-change audit"; ADR 0011 |
| Short-retention command audit | `README.md` Core Contracts |
| Work lands via a pull request the agent owns | commits `484baceb`, `44e81d5a` |
| Native merge queue, Zuul-style speculative train, AIMD window floor 1 / ceiling 4, eviction | commit `2b49fb23`; `src/mac/native_merge_queue.py` |
| "never land an untested tree" | commit `2b49fb23`: "THE INVARIANT, enforced structurally rather than asserted" |
| An entry that cannot progress must say so | commit `03da7f63` |
| Failure causes are classified | `src/mac/review_failure_classifier.py`, `src/mac/attempt_failure_classifier.py` |

## 4. Coordination (diagram 03)

All from commit `73476fc6` ("Make AgentBus a bus") unless noted.

| Claim | Source |
|---|---|
| Point-to-point messages are no longer private | "SEMANTIC CHANGE: … `_authorized` … now returns True for any agent on the bus" |
| The two near-miss incidents | "a `git add -A` moments from sweeping ~1,200 lines of another agent's half-finished work into an unrelated commit, and a `git commit -a` that did" |
| A convention agents could follow but not verify | "a rule agents can follow but cannot verify, because nothing tells one agent what another is doing" |
| The quoted rationale for not enforcing silence | "an enforced silence would stop an agent from volunteering the one fact that keeps another from destroying work" |
| Roll call and traffic endpoints | `GET /agents/{id}/agentbus/roll-call`, `GET /agents/{id}/agentbus/traffic` |
| Inbox is scoped by addressing, not access | "the inbox is scoped by ADDRESSING, not access" |
| `mac.debug.terminal.*` carve-out | `PARTICIPANT_SCOPED_TOPICS` consulted by `_may_read` |
| Typed schema registry, enforced at publish, advisory for unknown | `src/mac/agentbus_schemas.py` module docstring |
| Lifecycle verbs and scopes | `src/mac/agentbus_control.py`: `LIFECYCLE_VERBS` = stand_down, abort, pause, resume, status; `LIFECYCLE_SCOPES` = fleet, project, agent; task scope added in commit `454f48bb` |
| `abort` is flagged destructive | `LIFECYCLE_DESTRUCTIVE_VERBS = frozenset({LIFECYCLE_ABORT})` |
| Stand-down is not abort | `agentbus_control.py`: "A single button labelled 'stop' that means ABORT destroys work in flight; one that means PAUSE does not stop a runaway." |
| The console speaks on the bus rather than mutating | commit `e0136d06`: "it never mutates the control plane directly, it speaks on the bus like any other participant" |
| An unknown verb fails where built | `LifecycleVerbError` docstring |
| A directive with no target is refused | commit `e0136d06` body |
| GitHub merge queues are organization-only, HTTP 422 verified | commit `2b49fb23`: "adding a `merge_queue` rule to ruleset 20954943 returns HTTP 422 `Invalid rule 'merge_queue'`" |
| Priority ageing prevents starvation | `docs/dispatch-priority-bias-audit.md`; `MAC_DISPATCH_PRIORITY_AGING_SECONDS` in `docs/review-strategy-experiments.md` |

## 5. Fleet and execution (diagram 04)

| Claim | Source |
|---|---|
| macOS nodes are host installs under launchd, not containers | `docs/adr/0015-macos-nodes-are-host-installs.md` — **Accepted**, 2026-08-17; supersedes the "macOS fleet nodes via Docker" amendment to ADR 0008 |
| Linux: native steward + containerized execution | `docs/adr/0012-hybrid-native-steward-containerized-execution.md`; ADR 0015 amends it so "the containerized-execution half now applies to Linux nodes only" |
| Node supervisors: launchd / systemd / supervisord | ADR 0012 Context: "a macOS host managed by launchd, Linux hosts managed by systemd, and Kubernetes pods managed by supervisord" |
| Docker Engine/Moby is the only container runtime | `docs/adr/0008-openshell-docker-engine-runtime.md` |
| OpenShell owns process trees, filesystem/network policy, sandbox lifecycle, normalized action events | `README.md` lineage section |
| HGX elastic capacity | `docs/hgx-elastic-capacity.md` |
| Five coding-agent routes | `src/mac/coding_agent.py`; commits `4f9bd4d1` (opencode), `885cd400`, `c4f90d0f` (pi) |
| Sandbox derives its coding agents from the router | commit `d696855f` |
| Preflight sentinel | `src/mac/coding_agent.py`, `PREFLIGHT_SENTINEL = "MAC_CODING_AGENT_SANDBOX_OK"` |
| A test asserts the sandbox's MCP config launches mac's server | commit `efb428e9` |
| Sandbox policy changes announced on the bus, acted on between tasks | commit `b3c35a55` |
| Reproducible runtime manifests with digests and secret-value checks | `README.md` Core Contracts |
| `tested-<inputs-sha>` image tag | `docs/adr/0016`, "Problem" — lists it among the deterministic gates that worked |
| Synchronized cutover | `docs/synchronized-fleet-cutover.md`, `docs/fleet-cutover-transaction-protocol.md` |
| A rollout can require a passing `eval_run` before promote | `README.md` Core Contracts, evaluation contract |
| Secrets are tenant-scoped handles with audit records and redacted output | `README.md` Core Contracts |
| Credentials never remain in the review checkout's origin URL | `docs/fleet-operational-learning.md` |
| Egress declared per project and per task | `mac project egress`, `mac task egress` in `docs/reference/cli.md` |

## 6. Measurement (diagram 05)

| Claim | Source |
|---|---|
| Review experiment assignment is deterministic from `(experiment_id, task_id, policy_version)` | `docs/review-strategy-experiments.md` |
| The optimizer emits a policy candidate, never an automatic change | `docs/review-strategy-experiments.md`; `docs/scientific-optimizer.md` |
| Dreaming gates: provenance, contradiction reduction, privacy, retrieval quality, compression, win balance | `docs/dreaming-rewrite.md`, "Gates" |
| Compression gate quarantines output above 0.75× input; promotion retires superseded rows | `docs/dreaming-rewrite.md` |
| Fleet operational learning changes reviewer routing on proven success/failure | `docs/fleet-operational-learning.md` |
| Eval sets carry scoring direction, baseline and regression threshold | `README.md` Core Contracts |
| Hub event-loop stall detection | commit `44cc3b2a` |
| **28,352 `llm.route` events; 481.8M input / 5.05M output; 64% streaming; 29.5% null `input_tokens`; 0 cached** | `docs/adr/0017-token-spend-is-metered-at-the-router.md`, "Measured over the seven days to 2026-08-19" |
| Cost is priced at read time and reports say missing, not zero | ADR 0017: "`estimate_route_cost()` … prices `resolved_model` against a models catalog at read time and returns `(cost, was_priceable)`" |
| **Ledger census 2026-08-19: blocked 355, waiting 19, failed 2,105, cancelled 3,531, open 84, running 2** | `docs/adr/0018-task-graph-progressive-disclosure.md` |
| **165 of 355 blocked tasks wait on dependencies that can never complete** | ADR 0018, citing `task_9f3b80b8` |
| "A state whose p50 dwell is measured in days is not a queue, it is a graveyard" | ADR 0018, quoting `StuckView`'s own docstring |
| ADR 0016 session figures (764,172 tokens; 52 reviews / 38 tasks / 0 findings; 18 ready / 3 idle / 0 assignments) | `docs/adr/0016-agent-initiated-review.md`, "The evidence from one session (2026-08-16/17)" |

## 7. Statuses, stated precisely

| ADR | Status in the file |
|---|---|
| 0012 native steward + containerized execution | **Accepted; implementation deferred pending fleet measurement** |
| 0015 macOS nodes are host installs | **Accepted** |
| 0016 agents decide what a task needs | **Proposed** (2026-08-17) |
| 0017 token spend metered at the router | **Proposed** (2026-08-19) |
| 0018 task view is a graph | **Proposed** (2026-08-19) |

## 8. Where the README is stale

Noted because the deck contradicts it deliberately, and because it is worth fixing separately:

- `README.md` still documents **`src/mac/_hermes` as a vendored Hermes Agent 0.15.1 snapshot**
  supplying "the agent loop, gateways, tools, plugins, and skills". That directory does not exist
  at this commit — verified with `ls -d src/mac/_hermes` → *No such file or directory*. The vendored
  tree was removed in commit `3ebde2dd`, its fate recorded in `d43996c8`, and the Hermes persona
  plugin removed in `42897e08`.
- `README.md` Core Contracts still lists a **`mac-hermes` adapter** among the durable contracts.

The deck therefore does not describe Hermes as a live component.
