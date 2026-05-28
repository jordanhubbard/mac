---
id: mac-uic
status: closed
deps: []
links: []
created: 2026-05-20T16:06:21Z
type: task
priority: 1
mac-task-id: pending:mac-uic
---
# Extract Beads bridge behind a dedicated service boundary

The Beads bridge mixes CLI execution, repository polling, issue parsing, task import, state sync, and dirty-tree recovery inside ControlPlane. That makes it hard to test the bridge independently and easy for task lifecycle changes to leak into repository synchronization behavior.

## Acceptance Criteria

Beads CLI execution, ready-issue parsing, task import, and task-to-Beads sync are separated behind a BeadsBridgeService boundary; command execution is injectable in tests; ControlPlane delegates to the bridge and receives explicit result objects instead of mutating task lifecycle state directly.

## Notes

mac task task_0d4d0931f4c84e85bed296e5b9a0c530 failed: beads_failed_task_retry_limit
mac task task_0d4d0931f4c84e85bed296e5b9a0c530 failed: beads_failed_task_retry_limit
mac task task_0d4d0931f4c84e85bed296e5b9a0c530 failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
