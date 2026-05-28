---
id: mac-6cf
status: closed
deps: []
links: []
created: 2026-05-20T16:06:49Z
type: feature
priority: 2
mac-task-id: pending:mac-6cf
---
# Add workflow-to-task dry-run preview before start

Starting a workflow currently gives the human no UI-level preview of the exact tasks, dependencies, review gates, target roles, and task metadata that will be created. That makes workflow authoring hard to trust and harder to debug before the fleet starts doing work.

## Acceptance Criteria

A preview endpoint and UI panel show the exact task graph that will be created from a workflow or approved draft; preview includes dependencies, roles/capabilities, review/publication gates, repository targets, and inherited context; starting the workflow uses the previewed snapshot or clearly invalidates stale previews.

## Notes

mac task task_a0fb2ed3dac44bbe8d6455acecb7a73d failed: beads_failed_task_retry_limit
mac task task_a0fb2ed3dac44bbe8d6455acecb7a73d failed: beads_failed_task_retry_limit
mac task task_a0fb2ed3dac44bbe8d6455acecb7a73d failed: beads_failed_task_retry_limit
mac task task_a0fb2ed3dac44bbe8d6455acecb7a73d failed: beads_failed_task_retry_limit
mac task task_a0fb2ed3dac44bbe8d6455acecb7a73d failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
