# ADR 0034 - Project Mission Control is an additional observe view, not the IDE

- Status: **Accepted**
- Date: 2026-08-28
- Decision owner: MAC fleet owner
- Related: ADR 0018 (task view is a graph under progressive disclosure),
  ADR 0025 (the hub UI is the observability console)

## Context

ADR 0018 refused a fleet-wide node-and-edge canvas as the *primary* task view:
thousands of blocked tasks with cross-cutting edges are a hairball, and the
operator's usual question is local. It explicitly reserved an additional
visual canvas for later, with its own justification, that would not replace
the table spine.

A Cursor-only canvas of explorer + SVG DAG + inspector proved to be the right
shape for operating one project's live DAG (on the order of a hundred tasks,
not six thousand). That canvas was a snapshot. Operators need the same layout
against the ledger, in the hub UI.

Shipping `ide/` at `/ui` is still forbidden (ADR 0025). `ide/` mutates, and it
is not what the fleet serves.

## Decision

Add **Mission Control** to the observability console (`observe/`), read-only.

1. **Additional view.** Live, Stuck, Projects, and the rest stay. Mission
   Control is a Fleet-group rail entry and a deep-link from a project name.
2. **One project, capped.** `GET /dashboard/observe/projects/{project}/graph`
   returns schema `mac.dashboard.observe.project_graph.v1`. Live tasks fill a
   hard cap first (~200). Omitted rows and edges are named. There is no
   unbounded read.
3. **The hub owns join policy.** Each edge carries the waiter's join policy
   and a verdict (`dead` / `pending` / `satisfied` / `settled` / `unknown`).
   A failed blocker under `all_success` is not drawn the same as a slow one.
4. **SVG, no new dependency.** `observe/` still depends only on `react` and
   `react-dom`. Layout is a small layered DAG in the console itself.
5. **Honesty and GET-only extend unchanged.** A graph the hub could not read
   is omitted and shown as unavailable, never as an empty DAG. The console
   still cannot issue POST.

## Consequences

- Operators can watch a project's real DAG at `/ui` without opening a snapshot
  or the unshipped IDE.
- Projects too large for the cap still have an honest truncated view rather
  than a silent subset.
- ADR 0018's table-plus-disclosure neighbourhood remains the planned answer
  to "why is this one task not moving" at fleet scale; Mission Control does
  not replace that work.
