---
id: mac-iez
status: closed
deps: []
links: []
created: 2026-05-20T04:49:03Z
type: bug
priority: 0
mac-task-id: pending:mac-iez
---
# Auto-review: /reviews/default/tick endpoint scope is too broad (write can auto-merge)

api.py:1848 /reviews/default/tick falls through _required_scope to 'write' — the same scope used to create tasks. Any token that can author tasks can flush every NEEDS_REVIEW/REVIEWING task in the system to COMPLETED. Under the autonomous-swarm framing the question is which automation can call it: either restrict to admin so only the control plane drives ticks from a scheduled job, or add a dedicated 'review' scope. write is too broad — a task-author token should not be able to push tasks through the publication boundary.

## Close Reason

Closed
