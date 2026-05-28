---
id: mac-p2j
status: closed
deps: []
links: []
created: 2026-05-20T16:06:26Z
type: task
priority: 2
assignee: agent_natasha
mac-task-id: pending:mac-p2j
---
# Break fleet deployment script into structured deploy modules

deploy/deploy-mac-fleet.sh has become a large second application with host orchestration, generated service files, embedded Python, Hermes patching, bootstrap logic, and migration behavior in one shell surface. This is brittle to refactor and hard to review because small deployment changes require reasoning over the whole script.

## Acceptance Criteria

The shell entry point is reduced to host orchestration; reusable deployment behavior moves into versioned Python modules or small deploy scripts; service wrappers are rendered from templates; tests validate template rendering, per-host planning, and migration/bootstrap decisions without requiring a live fleet.

## Notes

mac task task_471f659cb8e747fdb63eef8adba2eb39 failed: beads_failed_task_retry_limit
mac task task_471f659cb8e747fdb63eef8adba2eb39 failed: beads_failed_task_retry_limit
mac task task_471f659cb8e747fdb63eef8adba2eb39 failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
