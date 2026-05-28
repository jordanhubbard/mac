---
id: mac-d2xh
status: closed
deps: []
links: []
created: 2026-05-27T00:54:32Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-d2xh
---
# FAILED→OPEN transition does not reset attempt_count — dead-letter requeue is no-op

src/mac/models.py:124-125 + services.py:4249-4251 — FAILED→OPEN and CANCELLED→OPEN transitions are allowed but no code path resets attempt_count or completed_at. A re-opened dead-letter task immediately re-FAILs on next claim because attempt_count>=max_attempts. completed_at also carries forward, so the task shows a stale completion time on inspection. Fix: reset attempt_count + completed_at on requeue, or expose explicit dead-letter retry primitive.

## Close Reason

Closed
