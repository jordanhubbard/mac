---
id: mac-m4a
status: closed
deps: []
links: []
created: 2026-05-23T04:51:50Z
type: task
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-m4a
---
# Harden Beads bridge canonical authority

Make canonical Beads DB output the only import and dispatch authority. JSONL export drift should be surfaced as repository health/repair information, not imported as work or silently ignored. Broken canonical reads should fail closed.

## Close Reason

Canonical Beads DB is now the only dispatch authority; JSONL drift/failures mark repositories unhealthy and repair endpoint is available
