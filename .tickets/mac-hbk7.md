---
id: mac-hbk7
status: closed
deps: []
links: []
created: 2026-05-27T00:55:07Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-hbk7
---
# Workflow snapshot does not freeze role definitions — mid-run role edits affect downstream nodes

src/mac/workflow_runtime.py:427 — _spawn_node_task calls self.roles.get_role(node['role_required'], tenant_id=tenant_id) reading the CURRENT role row. A mid-run role edit changes capability requirements and hardware constraints for downstream nodes even though the workflow definition was supposed to be deterministic. Fix: snapshot resolved role data into the workflow_runs row at start, OR document that role rows are part of the live behavioral contract.

## Close Reason

Closed
