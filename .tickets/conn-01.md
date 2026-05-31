---
id: conn-01
status: open
deps: []
links: [loop-01]
created: 2026-05-31T00:00:00Z
type: feature
priority: 2
audit: ticketing-connector-architecture
discovered_via: architecture_review
---
# EPIC: ticketing as a connector ("meta tickets"), not an embedded source

## Why this exists

Removing beads was painful because it had **no abstraction boundary**: beads
specific reads/writes/polling/dolt-sync were scattered across ~100 call sites in
the control-plane lifecycle (claim / evidence / publish / review / transition /
heartbeat / dispatch) + the API + dispatch + the deploy. Every ticketing system
is essentially the same shape (id, title, body, state, priority, deps), so the
rest of MAC should never see a *specific* system — it should see a `MetaTicket`
behind a `TicketingConnector`.

**Are mac tickets similarly embedded?** Partly. `.tickets/` + the `mac task`
ledger are the native source, and they're better-factored than beads was (the
ledger is a real store, the `.tickets` mirror is git-tracked). But the lifecycle
still references the native store directly rather than through the connector
seam. This epic makes the seam real so a project can use *any* ticketing system.

## Delivered (this change)

- `src/mac/ticketing.py`: `MetaTicket` + `TicketingConnector` ABC with three
  kinds — canonical (`NativeTicketingConnector` = .tickets + ledger),
  import-only (`BeadsImportConnector` = detect + one-way convert, **never** a
  read/write source), and writeback (the `on_task_*` lifecycle hooks, for a
  future Jira/GitHub/Linear connector that mirrors MAC into an external system).
- `detect_ticketing(repo_path)` → flags `needs_conversion` when a foreign
  source (.beads) is present but native `.tickets/` is not.
- `ControlPlane.detect_ticketing` / `convert_ticketing_source` + the
  `ticketing.conversion_available` observation hermes surfaces to ask the user.
- CLI: `mac task detect-ticketing` / `mac task convert-ticketing`.
- **beads removed as a read/write source**: heartbeat auto-poll + startup
  registration triggers, the `/bridge/beads/*` API + request models, the
  dispatch client methods, and the deploy install/bootstrap/restore machinery +
  `MAC_BEADS_*` env are all gone. beads is now ONLY the import-only connector.
- Tests: `tests/test_ticketing.py` (8). Removed 6 obsolete beads-source tests.

## Remaining work

- [ ] **Migrate the lifecycle onto the connector seam.** Replace the remaining
      direct beads sync calls (the gated `sync_*_side_effects` / `_sync_beads_*`
      / `_run_bd_for_task` / the `beads.*` transition-outbox events) with
      `self.ticketing.on_task_*` dispatch to registered connectors. With only
      the canonical (no-op) connector registered, these become true no-ops.
- [ ] **Excise the now-inert beads code** that's unreachable after this change:
      the gated beads sync/poll methods in `services.py`,
      `src/mac/beads_bridge_service.py`, the `beads_repositories` store table,
      the `BeadsRepository` model, and the ~28 beads-bridge tests that exercise
      those gated methods directly.
- [ ] **conn-02: a writeback connector** (Jira or GitHub Issues) implementing
      the `on_task_*` hooks + `import_tickets`, to prove the abstraction with a
      second real system.
- [ ] **conn-03: native connector as the real source of truth** — route the
      lifecycle's reads of `.tickets`/ledger through `NativeTicketingConnector`
      so even the native path is connector-mediated (no embedded assumptions).
