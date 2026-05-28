---
id: mac-25v
status: closed
deps: []
links: []
created: 2026-05-24T09:28:39Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-25v
---
# Add first-class Hermes object proof matrix

Runtime proof currently verifies the work-context schema and lifecycle operations, but UI projection is still a hardcoded readiness check and agent/project/task first-class coverage is not presented as a single auditable matrix. Add explicit proof evidence for tasks, projects, and agents across MAC API, MAC CLI, Hermes-facing CLI/runtime commands, dashboard projection, and direct-session capabilities; include missing evidence in readiness checks and cover it with tests.

## Close Reason

Added Hermes first-class object proof matrix for tasks, projects, and agents across API, CLI, UI projection, runtime capabilities, and Hermes-facing commands.
