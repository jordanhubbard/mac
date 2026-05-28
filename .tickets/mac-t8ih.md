---
id: mac-t8ih
status: closed
deps: []
links: []
created: 2026-05-27T00:54:13Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-t8ih
---
# Workflow _advance is not transactional — duplicate next-task spawns

src/mac/workflow_runtime.py:321-413 — _advance computes next_seq with one query, spawns the next task, then runs two more store.execute calls outside any transaction. No workflow_runs row lock, no state='running' guard on UPDATE, no UNIQUE conflict handler. Two terminal events on the same run can both pass the state-in-WORKFLOW_TERMINAL_STATES check at line 312 and both spawn next-node tasks; only the second history INSERT fails on UNIQUE(run_id, seq) at store.py:925, leaving a dangling orphan task with no history row. Fix: wrap in transaction with state guard.

## Close Reason

Closed
