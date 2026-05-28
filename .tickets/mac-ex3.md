---
id: mac-ex3
status: closed
deps: []
links: []
created: 2026-05-22T07:29:25Z
type: task
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-ex3
---
# Upgrade Beads CLI

Upgrade the local bd CLI and verify that the mac repository no longer hits the embedded Dolt dependency-schema warning during stats/import checks.

## Close Reason

Installed current Beads HEAD build 3a7a2e8 and verified bd stats/ready no longer fail on depends_on_id.
