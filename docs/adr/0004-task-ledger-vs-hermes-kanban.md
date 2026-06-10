# ADR 0004 — MAC task ledger vs. Hermes kanban: coexist with a thin delegation bridge

- Status: **Proposed**
- Date: 2026-06-09
- Decision owner: Jordan Hubbard
- Context: Phase 2 of `kanban-adopt-01`. We decided to **adopt** the Hermes
  kanban dashboard rather than re-implement a board in the mac dashboard
  (Phase 1 shipped a link-out, PR #122). That forces the question this ADR
  answers: how should the Hermes kanban store (`kanban.db`) relate to the MAC
  hub task ledger (`mac.db`, the `mac task` system)?

## TL;DR verdict

They are **different layers, not duplicates.** Keep both stores; do **not**
force them into one schema. Add a **thin one-way delegation bridge**: the mac
ledger stays the canonical fleet work-queue (system of record); a mac task may
*delegate* to a Hermes kanban board for goal-decomposition / swarm execution,
and the board's terminal outcome transitions the mac task. The dashboard shows
the ledger as primary and deep-links to the kanban board for delegated tasks.

## Ground truth (today, not assumptions)

| | **mac.db** task ledger | **kanban.db** Hermes kanban |
| --- | --- | --- |
| Purpose | Fleet **work-queue** | **Goal-decomposition + multi-agent swarm** |
| Model | `models.py` `Task`, `store.py` `tasks` | `_hermes/hermes_cli/kanban_db.py` `tasks` |
| Distinctive fields | `required_capabilities`, `lease_id`/`leased_until`, `owner_agent_id`, `max_attempts`, `project` | `board`, `workspace_kind`/`workspace_path`/`branch_name` (git worktrees), `task_links` (goal tree), `task_comments`/`task_events`, `task_runs`, `worker_pid`/`last_heartbeat_at`, `claim_lock` CAS |
| States | `open/blocked/claimed/running/needs_review/reviewing/completed/failed/cancelled` | `triage/todo/scheduled/ready/running/blocked/review/done/archived` |
| Dispatch | Agent polls; hub leases by capability | Kanban dispatcher subprocess spawns workers (`HERMES_KANBAN_TASK`) |
| Scope / location | One **shared hub** DB per fleet (`~/.mac/mac.db`) | **Per-board local** SQLite (`~/.hermes/kanban.db`, `boards/<slug>/`) |
| Answers | "*Who* can do this, and what's the contract?" | "*What's the goal tree*, and are my workers alive?" |

**There is no bridge today.** Nothing in `src/mac/` (outside `_hermes/`) imports
`kanban_db`/`kanban_tools`; the kanban dispatcher never reads `mac.db`; the mac
dispatcher never reads `kanban.db`. The only links are an agent's optional
`hermes_instance_id` and the read-only UI hyperlink added in PR #122. The
schema divergence is intentional: capability-based fleet dispatch vs. worktree-
bound worker-lifecycle decomposition solve different problems at different
scales (fleet vs. single profile/goal).

## Options considered

1. **Unify into one store.** *Rejected.* The schemas, scales, and concerns
   diverge; migration is large and either bloats the ledger with worktree/worker-
   liveness columns or strips kanban's swarm semantics. High cost, high risk,
   little near-term payoff.
2. **Coexist, no bridge (status quo).** *Insufficient.* The two are invisible to
   each other: a fleet task that is "really" a kanban swarm has no link, and the
   dashboard can't show decomposition progress against the ledger item. This is
   the gap that makes the bespoke board feel like duplication.
3. **Coexist with a thin one-way delegation bridge.** *Recommended.* See below.

## Decision

Adopt **Option 3.** Roles:

- **mac ledger = system of record** for fleet work and its lifecycle. Every unit
  of fleet work is a mac task; capability dispatch, leasing, review, and
  cross-fleet visibility stay here.
- **Hermes kanban = an execution strategy a task may delegate to** when the work
  warrants decomposition + a worker swarm (the `decompose`/`specify`/`swarm`
  pipeline). It owns the *inside* of a delegated task: the goal tree, worktrees,
  and worker liveness.

**Bridge contract (one-way, ledger-owned):**
- A mac task records `metadata.kanban = {board_id, dashboard_url}` when delegated.
- Delegation creates/links a kanban board for that task; the kanban dispatcher
  runs the swarm as it does today (unchanged).
- A reconcile step maps the board's **terminal** state back to a mac task
  transition (`done` → `completed`, `archived/failed` → `failed`), idempotently.
- The dashboard renders the ledger as primary and, for a delegated task,
  deep-links to `<hermes-dashboard>/kanban?board=<id>` (extends PR #122). No
  re-implementation of the board UI.

Direction is strictly one-way: the ledger owns task lifecycle; kanban owns
intra-task decomposition. We do **not** sync arbitrary status both ways.

## Consequences

- **Pros:** each store keeps doing what it is good at; one pane of glass via the
  ledger + deep links; incremental and reversible (no migration); the bespoke
  `renderTasks` board can be retired without losing the fleet work-queue.
- **Cons:** a small bridge + reconcile to maintain; two stores to back up and
  observe; write-back can drift if a reconcile is missed (mitigated by making
  reconcile idempotent and re-runnable).
- **Non-goals:** migrating kanban into `mac.db`; teaching the ledger about
  worktrees/worker PIDs; two-way status sync.

## Follow-up work (tickets, not part of this ADR)

1. Bridge: `metadata.kanban.board_id` on mac tasks + an idempotent reconcile
   (board terminal → task transition). 
2. Dashboard: deep-link a delegated task to its board (extends `kanban-adopt-01`
   Phase 1). 
3. Decide the trigger for delegation (explicit `mac task delegate-kanban`, or a
   capability/heuristic). 
4. Once the bridge + link exist, retire the bespoke `renderTasks` lanes
   (`ui-modularize-01` can drop that view).

Until the bridge lands, Phase 1 (the link-out from PR #122) is the interim
surface and the two stores remain independent.
