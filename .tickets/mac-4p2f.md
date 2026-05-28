---
id: mac-4p2f
status: closed
deps: []
links: []
created: 2026-05-27T00:53:26Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-4p2f
---
# Beads close is fire-and-forget after mac publication commits

src/mac/services.py:6276, 9644-9661 — _sync_beads_close runs after the publication transaction and has no compensating action if bd close fails. mac records COMPLETED with a task.published history row; the bead may stay open. No retry queue or reconciliation loop is visible. Fix: enqueue a durable bd-close intent inside the publication tx; reconcile on a timer.

## Close Reason

Closed
