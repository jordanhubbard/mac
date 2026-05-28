---
id: mac-eow
status: closed
deps: []
links: []
created: 2026-05-20T04:50:12Z
type: task
priority: 0
mac-task-id: pending:mac-eow
---
# Test: pin renew_lease's strict-refusal behavior on transitioning tasks

services.py:973 renew_lease was tightened to refuse when the underlying task isn't in CLAIMED/RUNNING — previously it would silently update. Only the positive path (BUSY liveness refresh) is pinned today. Add a negative regression test: claim a task, submit-for-review (moves to NEEDS_REVIEW, lease released), then call renew_lease — expect ValidationError 'lease is no longer attached to an active task'. Without this test a future revert quietly unbreaks the old footgun.

## Close Reason

Closed
