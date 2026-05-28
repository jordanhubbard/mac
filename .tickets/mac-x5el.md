---
id: mac-x5el
status: closed
deps: []
links: []
created: 2026-05-27T00:54:27Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-x5el
---
# No UNIQUE constraint preventing two ACTIVE leases per task

src/mac/store.py:221-233 — idx_leases_task_status is non-unique. The claim-task UPDATE relies on tasks.lease_id IS NULL for mutual exclusion, but the leases INSERT itself does not enforce 'at most one ACTIVE lease per task'. A bug or manual fix flipping a lease back to ACTIVE silently produces duplicates. Fix: partial unique index WHERE status='active'.

## Close Reason

Closed
