# ADR 0004 — One task database: revert the Hermes kanban adoption, keep our own board

- Status: **Accepted**
- Date: 2026-06-09
- Decision owner: Jordan Hubbard
- Context: Phase 2 of `kanban-adopt-01`. Phase 1 shipped a dashboard link-out to
  the Hermes kanban (PR #122). This ADR answers how the Hermes kanban store
  (`kanban.db`) should relate to the MAC hub task ledger (`mac.db`, the
  `mac task` system) — and concludes by reversing the Phase-1 adoption.

## TL;DR verdict

**There must be exactly one task database — the MAC task ledger (`mac.db`).**
We evaluated making Hermes' kanban read from that ledger (a pluggable task
store). It is **not feasible cheaply**: `kanban_db.py` is a 7,386-line concrete
SQLite module with no store seam, ~200 raw-SQL call sites, 7 tables, and
SQLite-specific CAS/migration machinery — and it is vendored upstream, so a
semantic rewrite is exactly the heavy fork ADR 0001 tells us not to carry.

Per the decision rule ("if Hermes can't be patched to a pluggable task store
*easily*, revert and keep our own"): **revert the Hermes kanban adoption** (the
Phase-1 dashboard link) and keep the bespoke board (`renderTasks`) as the single
task UI over the single store. An earlier draft of this ADR proposed coexisting
with a bridge; that is **rejected** in favor of one store.

## Ground truth (today, not assumptions)

| | **mac.db** task ledger | **kanban.db** Hermes kanban |
| --- | --- | --- |
| Purpose | Fleet **work-queue** | **Goal-decomposition + multi-agent swarm** |
| Model | `models.py` `Task`, `store.py` `tasks` | `_hermes/hermes_cli/kanban_db.py` `tasks` |
| Distinctive fields | `required_capabilities`, `lease_id`/`leased_until`, `owner_agent_id`, `max_attempts`, `project` | `board`, `workspace_kind`/`workspace_path`/`branch_name` (git worktrees), `task_links`, `task_comments`/`task_events`, `task_runs`, `worker_pid`/`last_heartbeat_at`, `claim_lock` CAS |
| States | `open/blocked/claimed/running/needs_review/reviewing/completed/failed/cancelled` | `triage/todo/scheduled/ready/running/blocked/review/done/archived` |
| Location | One **shared hub** DB per fleet (`~/.mac/mac.db`) | **Per-board local** SQLite (`~/.hermes/kanban.db`) |

No bridge exists today: nothing in `src/mac/` (outside `_hermes/`) imports
`kanban_db`; neither dispatcher reads the other's DB; the only links were agent
identity and the PR #122 hyperlink (now reverted).

## Feasibility of a pluggable kanban store (the decision gate)

Measured against `src/mac/_hermes/hermes_cli/kanban_db.py`:

- **7,386 lines**, **no abstraction seam** — no `Protocol`/`ABC`/`Store`; the
  classes are data records, not a swappable backend.
- **Every function takes `conn: sqlite3.Connection`** and runs raw SQL; **~200**
  `execute`/`BEGIN IMMEDIATE`/`ON CONFLICT`/`RETURNING` sites; plus
  `_migrate_add_optional_columns`, `_table_has_drifted`, `_rebuild_drifted_tables`,
  `_check_file_length_invariant` — all SQLite-specific.
- **Vendored upstream.** Reworking the data layer is a large fork of a
  fast-moving dependency, contradicting ADR 0001's "pinned, pruned snapshot; no
  semantic surgery" guidance.

Both routes to "kanban on mac.db" are expensive: (a) introduce a store interface
and rewrite ~200 call sites in vendored code, or (b) recreate all 7 kanban tables
inside `mac.db` (which is *hosting* the kanban schema, not *one* tasks table).
Neither is "easy." → revert.

## Decision

1. **`mac.db` is the single task database.** All fleet work is `mac task`.
2. **Revert the Phase-1 Hermes kanban adoption** — remove the dashboard kanban
   service link (PR #122). Keep the bespoke `renderTasks` board (with CEO-mode
   creation, #119, and the refresh fix, #121) as the single task surface.
3. **Do not fork Hermes kanban.** If the fleet later wants kanban-style
   decomposition/swarm, build it **natively against `mac.db`** (a board *view* +
   parent/child task links over the existing ledger), not by adopting a second
   store.

## Consequences

- **Pros:** one store, one source of truth, no second DB to back up/observe, no
  heavy vendored fork to carry. The board we keep already works (refresh fix +
  CEO mode shipped).
- **Cons:** we forgo Hermes' ready-made swarm/decompose UI; any such capability
  is now ours to build on the ledger.
- **Reversal:** `kanban-adopt-01` is closed as **won't-adopt**; reopen only if
  Hermes upstream grows a pluggable task-store seam that removes the fork cost.

## Follow-up

- Closes `kanban-adopt-01` (won't-adopt). 
- Optional, native: parent/child links + a board-style lens over `mac.db` tasks
  if decomposition UX is wanted later — tracked separately, not by adopting
  `kanban.db`.
