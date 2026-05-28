---
id: mac-3nf
status: closed
deps: []
links: []
created: 2026-05-21T22:29:59Z
type: bug
priority: 1
assignee: codex
mac-task-id: pending:mac-3nf
---
# Fallback Beads writeback when bridge checkout DB is broken

A failed or stale disposable Beads bridge checkout can prevent mac from writing failure summaries and ledger comments back to the operator-visible Bead. Fall back to the registered checkout for writeback so failures remain visible.

## Acceptance Criteria

When bd fails in the bridge checkout and the registered checkout is different and usable, mac retries the same Beads write there, logs the fallback, and tests cover the failure mode.

## Close Reason

Fixed by retrying Beads sync writeback in the registered checkout when the managed bridge checkout's bd command fails; added regression coverage and verified full pytest suite.
