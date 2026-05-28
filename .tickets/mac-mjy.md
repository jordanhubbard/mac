---
id: mac-mjy
status: closed
deps: []
links: []
created: 2026-05-20T18:53:39Z
type: task
priority: 1
mac-task-id: pending:mac-mjy
---
# Create targeted remediation tasks for dirty Beads source

Dirty or refresh-failed registered Beads source should create an idempotent remediation task for the agent that owns the environment, rather than only blocking with telemetry. The remediation task must be target-agent constrained and instruct the owner to fetch, pull with rebase/autostash or equivalent, reconcile local changes, run relevant contract tests, and leave the checkout clean.

## Close Reason

Implemented targeted dirty-source remediation tasks with dispatcher target-agent gating and tests
