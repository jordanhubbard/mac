---
id: mac-sce
status: closed
deps: []
links: []
created: 2026-05-20T06:01:14Z
type: feature
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-sce
---
# Add Beads-to-mac task bridge heartbeat poller

Registered repositories with Beads issue databases need to be polled by the Rocky hub so newly ready/open beads become durable mac tasks automatically instead of requiring this session to import or execute them manually.

## Acceptance Criteria

mac stores registered Beads repositories; a bridge poll imports ready/open Beads into project_items/tasks idempotently; hub heartbeat runs the poller on an interval; imported tasks are claimable by fleet workers; CLI/API/tests/docs cover registration, polling, idempotency, and deploy enablement.

## Close Reason

Implemented registered Beads repository bridge with idempotent ready-Bead import, API/CLI poll controls, Rocky heartbeat polling enablement, deploy env defaults, docs, and tests.
