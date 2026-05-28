---
id: mac-d2x
status: closed
deps: []
links: []
created: 2026-05-21T22:15:45Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-d2x
---
# Mirror failed task causes into Beads

Failed mac tasks currently leave the Bead showing only the original goal. Mirror concise failure causes, evidence ids, verification problems, and retry exhaustion into Beads notes/comments so bd show explains why an open Bead is not progressing.

## Acceptance Criteria

Failed Beads-backed tasks append a mac failure summary to the Bead; retry-exhausted failed tasks backfill the summary idempotently during bridge reconciliation; the summary includes reason/problems/evidence when present; tests cover future failures and retry-exhausted backfill.

## Close Reason

Mirrored failure causes into Beads notes/comments for failed tasks, added retry-exhausted backfill, and covered the sync path with regression tests.
