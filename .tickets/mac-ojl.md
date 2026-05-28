---
id: mac-ojl
status: closed
deps: []
links: []
created: 2026-05-23T19:59:54Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-ojl
---
# Make default review requests idempotent per reviewer

Concurrent review workflow ticks can request two pending reviews for the same task and reviewer, which leaves the task ambiguous after executor evidence is submitted. Make request_review reuse an existing pending same-reviewer review and have the default workflow retract duplicate same-reviewer pending rows while preserving ambiguity for distinct reviewers.

## Close Reason

Fixed: request_review now reuses pending same-reviewer reviews and default review workflow retracts duplicate same-reviewer pending rows.
