# Audit — every claim in this deck, traced to source

Audited at commit `bac50778` on 2026-08-20T18:23:40Z by reading the tree, plus live measurements
from a running hub taken the same day. Generated references (`docs/reference/cli.md`,
`docs/reference/openapi.md`) are authoritative for surface counts because CI fails when they drift
from the live parser and OpenAPI schema.

---

## 1. Counts (slide 8)

| Claim | Source |
|---|---|
| 198,038 lines of Python under `src/` | `find src -name "*.py" \| xargs wc -l` |
| 205 modules in `src/mac` | `ls src/mac/*.py \| wc -l` |
| 483 test files | `ls tests/*.py \| wc -l` |
| 408 HTTP routes | `grep -cE "^\| \`(GET\|POST\|PUT\|PATCH\|DELETE)\`" docs/reference/openapi.md` |
| 124 CLI verbs | Parsed from `docs/reference/cli.md`: task 45, admin 53, agent 17, project 9 |
| 31 hub services | `ls src/mac/*_service.py \| wc -l` |
| 24 ADRs, 11 still Proposed | `ls docs/adr/*.md`; `grep -l "Status: \*\*Proposed\*\*" docs/adr/*.md` |

## 2. Hub and workers (slide 2)

| Claim | Source |
|---|---|
| The hub keeps the ledger, the capability registry and capacity | `docs/adr/0016-agent-initiated-review.md`, "The hub keeps only durable, uncontested authority" |
| It does not decide what a task needs | Same ADR: "`required_capabilities` is a guess made before anyone has read the diff … information the executing agent holds and the hub does not." |
| macOS nodes are host installs under launchd | `docs/adr/0015-macos-nodes-are-host-installs.md` — **Accepted** |
| Linux: native steward + containerized execution; OpenShell owns isolation | `docs/adr/0012-hybrid-native-steward-containerized-execution.md`; ADR 0015 narrows the containerized half to Linux; `README.md` lineage for OpenShell's scope |
| Kubernetes pods under supervisord; HGX elastic | ADR 0012 Context; `docs/hgx-elastic-capacity.md` |
| Merge queue: speculative train, eviction discards followers, AIMD window floor 1 / ceiling 4 | commit `2b49fb23`; `src/mac/native_merge_queue.py` |
| Agents land through a PR they own, never a push | commits `484baceb`, `44e81d5a` |
| AgentBus is broadcast; the human is a participant | commit `73476fc6`; `src/mac/agentbus_control.py` (`LIFECYCLE_VERBS`, and "it never mutates the control plane directly, it speaks on the bus like any other participant") |
| stand_down is not abort | `src/mac/agentbus_control.py`: "A single button labelled 'stop' that means ABORT destroys work in flight; one that means PAUSE does not stop a runaway." |
| **Live:** 10 agents; 2 busy / 2 idle / 6 offline; 6 static / 4 fungible; 6 GPU-capable | `mac agent list` against the running hub, 2026-08-20 |

## 3. The life of one task (slide 3)

| Claim | Source |
|---|---|
| Eleven states, three terminal | `src/mac/models.py`, `class TaskState`, `TERMINAL_TASK_STATES` |
| Leases and fences authorize mutation; lease expiry is the single liveness mechanism | `README.md` Core Contracts; ADR 0016 "What must not be lost — Liveness" |
| Evidence is typed and bound to one exact attempt | `README.md` Core Contracts; `docs/review-strategy-experiments.md` |
| Approval needs a different agent AND a different model | `docs/review-strategy-experiments.md` |
| An unanswered peer review completes without review rather than waiting | ADR 0016, "Liveness" |
| Eviction discards results projected behind the evicted entry | commit `2b49fb23`: those entries "were green against a state that will never exist" |
| 52 reviews across 38 tasks, four distinct reason strings, zero findings | ADR 0016, "The evidence from one session (2026-08-16/17)" |
| **ADR 0016 is Accepted, dated 2026-08-20** | `docs/adr/0016-agent-initiated-review.md` header. It was `Proposed` when the previous deck was built; re-read rather than recalled. |

## 4. Inside the hub (slide 4)

All from `src/mac/api.py` and `src/mac/services.py` at this commit.

| Claim | Source |
|---|---|
| Exactly three daemon threads | `api.py`: `threading.Thread(... name="mac-hub-tick")`, `"mac-publication"`, `"mac-retention"` |
| The tick is the self-driver, and nothing drove it before | `_start_hub_tick_loop` docstring: "Nothing drove it periodically, so approved tasks parked forever waiting for an external `POST /dispatch/tick`" |
| Gated by `MAC_HUB_TICK_INTERVAL_SECONDS` so the CLI/tests/replicas do not compete | same docstring |
| The publication sweep clones a repo and runs a sandboxed contract gate | `_start_publication_worker` docstring |
| Heartbeats measured at 250–315s, lease renewals at 33s | same docstring: the sweep ran "on a WORKER'S HEARTBEAT REQUEST THREAD, so the cost landed on the worker" |
| Tick order: stale agents → ephemerals → leases → workflows → service roles → unblock/auto-retry → retention → dispatch | `ControlPlane.tick()` in `services.py`, read in sequence |
| Retention runs before the sweep, deliberately | `tick()` comment: "Retention runs HERE -- before the review sweep -- and not further down" |
| Zero retention events in 48 minutes; 235,615 observability rows and 5,576 action events past cutoff | same comment, observed live 2026-08-17 |
| Retention's failure is invisible because it is silent when idle; the store reached 16GB / 10.4M rows | same comment |
| 31 services, grouped six ways | `ls src/mac/*_service.py`; the grouping is editorial, the membership is not |
| Memory must shrink to be promoted (0.75× gate) | `docs/dreaming-rewrite.md` |
| Secrets are handles; output redacted; manifests check for baked-in values | `README.md` Core Contracts |

## 5. The console captures (slides 5–7)

Captured 2026-08-20 from a live hub at `/ui`, which accepts a bearer token as `?t=`
(`observe/src/App.tsx`, `readToken`).

| Claim | Source |
|---|---|
| `/ui` is read-only observability; the command-and-control dashboard was retired | commits `d725fa12`, `43887f68`, `27ed8af9` |
| "Status not believable" counts agents reporting idle/busy but unheard >15m | visible in the capture; the tile states its own definition |
| 644 tasks in flight, 465 of them blocked | live capture, `live` view, 6h window |
| Two merge queues, 4 waiting, 2 landed, 211 evicted, 1% land rate | live capture, `merge-queue` view |
| Every eviction reason is "projected merge conflicts with the queue base" | live capture, "why changes did not land" panel |
| 165 of 355 blocked tasks wait on dependencies that can never complete | `docs/adr/0018-task-graph-progressive-disclosure.md`, citing `task_9f3b80b8` — dated 2026-08-19, one day before these captures |

**Identity handling.** The `agents` view lists real agent names, several of which are tokens
`tests/test_docs_no_operator_identity.py` forbids in checked-in docs. That capture is cropped to
its top stat tiles before use, and the deck says so. Repository names in the merge-queue capture
are left visible: that test explicitly permits the repo-org slug and forbids only operator and
fleet identity.

## 6. Where this deck disagrees with the last one

The previous deck (`20260820T011224Z-8b424c20`) recorded that the README described a vendored
Hermes tree that had been deleted. That has since been fixed, and this deck makes no such claim.

ADR 0016 moved from **Proposed** to **Accepted** between the two decks — same repository, nine
hours apart. That is the argument for pinning a deck to a commit rather than editing one forever.
