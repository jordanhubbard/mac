---
id: mac-pykd
status: closed
deps: []
links: []
created: 2026-05-27T00:55:09Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-pykd
---
# Workflow max_attempts is parsed but never enforced

src/mac/workflow_models.py:40 enforces max_attempts >= 1 in the schema, but src/mac/workflow_runtime.py:384 _spawn_node_task always passes attempt=1 and never retries on failure. A node that fails transiently goes straight to the failure edge. Fix: implement per-node retry loop with backoff; on failure, increment attempt and re-spawn until max_attempts.

## Close Reason

Closed
