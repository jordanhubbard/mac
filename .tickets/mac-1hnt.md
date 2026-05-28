---
id: mac-1hnt
status: closed
deps: []
links: []
created: 2026-05-27T00:54:25Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-1hnt
---
# No CHECK constraint or trigger enforces task state machine

src/mac/store.py:150-169 — tasks.state is plain TEXT; illegal transitions are blocked only by validate_transition in Python (models.py:85-126). Any direct UPDATE or bug in the dozen call sites that mutate tasks.state can introduce illegal states with no DB safety net. Fix: CHECK constraint over allowed states; trigger validating from→to pairs.

## Close Reason

Closed
