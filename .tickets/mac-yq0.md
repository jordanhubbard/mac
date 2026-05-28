---
id: mac-yq0
status: closed
deps: []
links: []
created: 2026-05-20T16:05:47Z
type: task
priority: 1
mac-task-id: pending:mac-yq0
---
# Extract task ledger and dispatch out of ControlPlane

ControlPlane is still the dominant coordination object for task ledger behavior, dispatch, review workflow hooks, Beads sync, verification, command audit, and callback wiring. This is the same shape that made ACC hard to evolve: a change in one workflow can accidentally couple to unrelated operational behavior.

## Acceptance Criteria

Task lifecycle behavior lives behind a TaskLedgerService boundary; claim/dispatch policy lives behind a DispatchService boundary; ControlPlane is reduced to composition and compatibility delegation; existing API and worker tests pass without public API contract changes.

## Notes

mac task task_4dff69b6ecf64abf8d4dc709849d2a26 failed: beads_failed_task_retry_limit
mac task task_4dff69b6ecf64abf8d4dc709849d2a26 failed: beads_failed_task_retry_limit
mac task task_4dff69b6ecf64abf8d4dc709849d2a26 failed: beads_failed_task_retry_limit
mac task task_4dff69b6ecf64abf8d4dc709849d2a26 failed: beads_failed_task_retry_limit
mac task task_4dff69b6ecf64abf8d4dc709849d2a26 failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
