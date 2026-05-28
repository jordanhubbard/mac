---
id: mac-if8
status: closed
deps: []
links: []
created: 2026-05-21T22:38:20Z
type: bug
priority: 1
assignee: codex
mac-task-id: pending:mac-if8
---
# Push Beads writebacks after mac ledger updates

mac can update Beads notes/comments in a disposable or registered checkout without pushing the Dolt database, leaving operator-visible bd show output without the failure causes mac believes it recorded.

## Acceptance Criteria

Successful Beads writebacks run bd dolt push; push failure is logged and treated as writeback failure so failure summaries are retried; tests cover push success and failure.

## Close Reason

Fixed by pushing Beads/Dolt state after every successful mac Beads writeback, logging push failures, and leaving summaries retryable when the push fails.
