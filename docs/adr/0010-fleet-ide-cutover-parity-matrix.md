# ADR 0010 — Fleet IDE Cut-over and Parity Matrix

**Status:** Accepted  
**Date:** 2026-06-27

---

## Context

The fleet ships two browser/desktop UI surfaces:

| Surface | Location | Status |
|---------|----------|--------|
| Legacy dashboard | `src/mac/ui/` | Maintenance-only (frozen) |
| Fleet IDE | `ide/` | **Canonical — active development** |

The legacy dashboard (`src/mac/ui/`) was the original single-page dashboard
for the MAC fleet hub. It was built as a progressively enhanced vanilla
TypeScript/JavaScript application served by the MAC Python API.

The Fleet IDE (`ide/`) is the greenfield replacement: a fully featured browser
and desktop UI built on modern tooling (Vite, TypeScript, component
architecture) with richer capabilities: multi-panel layout, integrated
terminal, project/task editors, real-time streaming, and full Electron desktop
support.

---

## Decision

The Fleet IDE (`ide/`) is the **canonical browser and desktop UI** for all MAC
fleet workflows. `src/mac/ui/` enters **maintenance-only (frozen)** status
immediately:

- No new features may be added to `src/mac/ui/`.
- Critical bug fixes (security, data-loss, broken authentication) are still
  permitted; cosmetic and behavioral improvements are not.
- All new UI work must target `ide/`.

The freeze is enforced by:

1. Deprecation comment blocks at the top of every source file in
   `src/mac/ui/` (added in the same commit that introduced this ADR).
2. An automated test in `tests/ui/test_ui_freeze.py` that asserts the
   `DEPRECATED` marker is present in `app.ts`, `app.js`,
   `dashboard_api.ts`, and `dashboard_api.js`. The contract test suite will
   reject any PR that removes the marker.

---

## Parity Matrix

The table below tracks feature parity between the legacy dashboard and the
Fleet IDE. Items marked **Done** have been validated in `ide/`; items marked
**Planned** are on the IDE roadmap; items marked **N/A** are legacy-only
quirks that will not be ported.

| Feature | Legacy dashboard | Fleet IDE |
|---------|-----------------|-----------|
| Hub authentication (Bearer token) | Done | Done |
| Multi-target connection (browser / remote API / Electron) | Done | Done |
| Overview / summary panel | Done | Done |
| Task list + kanban | Done | Done |
| Task inspector + quick-actions | Done | Done |
| New-task form | Done | Done |
| Agent list + inspector | Done | Done |
| Project list + inspector | Done | Done |
| Workflow planner (plan/accept/cancel) | Done | Done |
| Runtime panel (deltas / runs / environments / rollouts) | Done | Planned |
| Observability streaming | Done | Planned |
| Debug terminal (xterm.js) | Done | Planned |
| Secret inspector + create | Done | Planned |
| Hermes config surface (fleet-level config, env, plugins, skills) | Done | Planned |
| Service-link sidebar | Done | Planned |
| Desktop packaging (Electron) | Done | Planned |
| Mobile responsive layout | Done | Planned |
| `?t=` URL-param token bootstrap (logs-safe) | Done | Done |
| Dark-mode theming | N/A | Done |

---

## Consequences

- Operators and contributors will see a prominent `DEPRECATED` banner at the
  top of every `src/mac/ui/` source file.
- The CI/CD contract test (`test_ui_freeze.py`) guards against accidental
  removal of the freeze notice.
- Teams adding UI features must target `ide/` exclusively; PRs adding
  new functionality to `src/mac/ui/` will be rejected during review.
- The legacy dashboard will continue to be served at `/ui` until the Fleet
  IDE reaches full parity and a migration cut-over task is scheduled.
