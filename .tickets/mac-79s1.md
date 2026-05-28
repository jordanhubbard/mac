---
id: mac-79s1
status: closed
deps: []
links: []
created: 2026-05-27T00:54:09Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-79s1
---
# release_lease has no status guard — TOCTOU clobber of new owner

src/mac/services.py:4720-4763 — release_lease UPDATE has no WHERE status='active' filter; the task UPDATE has no lease_id=? AND owner_agent_id=? filter. A stale agent calling release_lease after the lease already expired and the task was reclaimed will overwrite the task back to OPEN, clearing the new owner. Authorization check is pre-tx (TOCTOU). Fix: add status guards inside the transaction.

## Close Reason

Closed
