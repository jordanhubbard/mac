---
id: kanban-adopt-01
status: closed
deps: []
links: [ui-modularize-01]
created: 2026-06-09T00:00:00Z
type: epic
priority: 1
audit: dashboard-ui-review
discovered_via: architecture_review
resolution: wont-adopt
---
# Adopt the Hermes kanban plugin instead of the bespoke dashboard board

## Decision (2026-06-09): WON'T ADOPT — reverted

Superseded by ADR 0004. The fleet needs **one task database** (`mac.db`).
Making Hermes' kanban read from it requires reworking `kanban_db.py` — 7,386
lines of vendored SQLite with no store seam, ~200 raw-SQL call sites, 7 tables —
which is not feasible cheaply and would be a heavy fork of upstream
(contra ADR 0001). Per the decision rule, the Phase-1 adoption (the dashboard
kanban service link, PR #122) is **reverted** and the bespoke `renderTasks`
board is kept as the single task surface over `mac.db`. Any future
kanban-style decomposition UX should be built natively on the ledger, not by
adopting `kanban.db`. The original plan below is retained for history.

## Why this exists

The mac dashboard re-implements a task board (`renderTasks` in
`src/mac/ui/app.ts`, lanes over `TASK_STATES`) while the vendored Hermes runtime
already ships a complete kanban system. Decision (2026-06-09): adopt Hermes'
kanban rather than keep growing the bespoke board.

## What Hermes already provides

- **Board + store:** `hermes_cli/kanban_db.py` — SQLite `kanban.db`, multi-board,
  CAS status updates. Statuses: `triage/todo/scheduled/ready/running/blocked/
  review/done/archived`.
- **Coordination:** `hermes_cli/kanban_{decompose,specify,swarm,diagnostics}.py`,
  `tools/kanban_tools.py`, a dispatcher (`plugins/kanban/systemd/
  hermes-kanban-dispatcher.service`), and devops skills (kanban-worker/-orchestrator).
- **Dashboard plugin:** `_hermes/plugins/kanban/dashboard/` — `manifest.json`
  (tab `/kanban`, entry `dist/index.js`, css `dist/style.css`, api `plugin_api.py`)
  + `plugin_api.py` (93 KB FastAPI router, drag-drop board, comment threads,
  `/events` WebSocket, session-token auth). Mounted at `/api/plugins/kanban/`.

## The two gaps that make this non-trivial

1. **Frontend not vendored.** Only `plugin_api.py` + `manifest.json` are in this
   repo; the built React bundle (`dist/index.js`, `dist/style.css`) lives upstream
   (`NousResearch/hermes-agent`) and is lazily installed by `hermes dashboard`.
2. **Different server + different store.** The plugin targets the *Hermes*
   dashboard server (`hermes_cli/web_server.py`) with its own auth
   (`window.__HERMES_SESSION_TOKEN__`); the mac dashboard is served by the mac hub
   (`src/mac/api.py`, no plugin system). And the board reads **`kanban.db`**, a
   different store than the **mac task ledger** (`mac.db`, `mac task`) the current
   dashboard view shows. So "adopt the plugin" also forces a data decision:
   does the kanban view *supplement* the ledger view, or do the two task stores
   *unify*? (Ties to the Hermes-unification ADR.)

## Integration options

- **A — Link/embed the Hermes dashboard kanban.** Add a "Kanban" nav entry that
  opens the Hermes dashboard's `/kanban` (link or iframe). Zero UI
  re-implementation; needs the Hermes dashboard process running + its built
  frontend; auth/origin are separate.
- **B — Mount the plugin into the mac hub.** Include `plugin_api.py`'s APIRouter
  on the mac hub app, vendor/build the kanban `dist/` into the mac UI, adapt the
  plugin's session-token auth to the hub's bearer auth. One dashboard, one server,
  one auth — most work.
- **C — Reconcile stores first (ADR).** Decide the single source of truth
  (mac ledger vs kanban.db vs bridge) before any UI, since the board is only as
  useful as the data behind it.

## Recommended phasing

1. **Phase 1 (fast):** Option A — surface the existing Hermes kanban from the mac
   dashboard with a nav link, proving the real plugin end-to-end with no
   re-implementation. Keep the bespoke ledger board (with CEO-mode creation, #119)
   during the transition.
2. **Phase 2 (durable):** resolve the store question (C), then if warranted do B
   and retire `renderTasks`' bespoke lanes.

## Interim

If the bespoke board lives through the transition, fix its refresh regression:
the streamed-update `render()` replaces the DOM and wipes an open New Task drawer
+ half-typed prompt (worse now that CEO mode invites long prompts). Preserve the
drawer's open state + field values across re-renders.

## Notes

Surfaced during the dashboard UI review. Related: [[ui-modularize-01]].
