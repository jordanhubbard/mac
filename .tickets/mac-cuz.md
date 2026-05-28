---
id: mac-cuz
status: closed
deps: []
links: []
created: 2026-05-21T03:45:29Z
type: bug
priority: 1
mac-task-id: pending:mac-cuz
---
# Reconcile open Beads with failed MAC tasks

When a Bead remains open/ready but its mapped MAC task is terminal failed, the Beads bridge currently counts it as existing and leaves it unclaimable. Reopen failed mapped tasks with a bounded retry policy so ready Beads can progress again.

## Close Reason

Implemented bounded reconciliation for ready Beads whose mapped MAC tasks are failed, with retry-exhaustion telemetry and regression coverage.
