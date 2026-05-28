---
id: mac-qon
status: closed
deps: []
links: []
created: 2026-05-20T16:06:59Z
type: task
priority: 1
assignee: agent_natasha
mac-task-id: pending:mac-qon
---
# Introduce typed workflow definition models

Workflow definitions are currently stored and validated as raw dictionaries and lists. That makes workflow evolution depend on ad hoc shape checks and forces the UI, API, runtime, and validators to rediscover the same schema by convention.

## Acceptance Criteria

WorkflowDefinition, WorkflowNode, and WorkflowEdge are represented by typed models shared by API validation and runtime execution; versioned serialization remains backward compatible with existing rows; validation errors identify stable fields and paths for UI rendering; tests cover parse, validate, serialize, and runtime handoff.

## Notes

mac task task_e5892ae86381455ab97e7273f5a9bee5 failed: beads_failed_task_retry_limit
mac task task_e5892ae86381455ab97e7273f5a9bee5 failed: beads_failed_task_retry_limit
mac task task_e5892ae86381455ab97e7273f5a9bee5 failed: beads_failed_task_retry_limit
mac task task_e5892ae86381455ab97e7273f5a9bee5 failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
