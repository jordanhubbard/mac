---
id: mac-q0mq
status: closed
deps: []
links: []
created: 2026-05-27T00:54:53Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-q0mq
---
# No cycle detection on workflow import — bug.json already loops

src/mac/workflow_models.py:160-175 — WorkflowDefinition._validate_graph only checks that every node has an inbound edge; no cycle detection, no dead-end check, no unreachable-target check. src/mac/data/workflows/bug.json:78-86 loops pm_review → investigate → pm_review on rejection; the runtime walks live with no attempt cap (max_attempts is parsed at workflow_models.py:40 but workflow_runtime.py:384 always passes attempt=1 and never retries on failure). Fix: topological sort detect cycles, OR cap iterations per node-key per run.

## Close Reason

Closed
