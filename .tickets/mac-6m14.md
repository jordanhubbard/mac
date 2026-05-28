---
id: mac-6m14
status: closed
deps: []
links: []
created: 2026-05-27T00:53:28Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-6m14
---
# Command-audit argv stored verbatim and re-broadcast through observability

src/mac/services.py:3997-4019 — argv and metadata from /agents/{id}/command-audit are stored verbatim and re-emitted into observability_events.detail. Workers that put passwords/tokens on argv (a common antipattern) get cleartext written into command_audit.argv AND observability with independent retention. Fix: run a secret-pattern scrubber on argv before insert; reject high-entropy strings unless explicitly opted in.

## Close Reason

Closed
