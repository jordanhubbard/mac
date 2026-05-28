---
id: mac-24t
status: closed
deps: []
links: []
created: 2026-05-24T10:48:53Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-24t
---
# Prove live MAC object alignment for Hermes

The Hermes runtime proof checks contracts and declared operations, but it does not explicitly compare live MAC task, project, and agent records against the Hermes work-context projection. Add runtime proof evidence that live MAC task/project/agent identities align with the Hermes work-context view, including truncation-aware task checks and project/agent consistency checks, so first-class object coupling is proven from current MAC state rather than declarations alone.

## Close Reason

Added live MAC object alignment evidence to Hermes runtime proof, comparing current task/project/agent records against Hermes work-context projections, surfaced live alignment in the dashboard, documented it, and covered it in tests.
