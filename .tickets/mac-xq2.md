---
id: mac-xq2
status: closed
deps: []
links: []
created: 2026-05-20T16:06:39Z
type: feature
priority: 1
assignee: agent_natasha
mac-task-id: pending:mac-xq2
---
# Add workflow authoring dashboard views

The backend exposes workflow CRUD/import/start APIs, but the dashboard has no Workflows navigation or authoring surface. Humans currently cannot create, inspect, edit, validate, or start multi-step workflows from the UI.

## Acceptance Criteria

Dashboard includes a Workflows nav item; users can list, create, edit, validate, and start workflows; the manual step editor supports title, instructions, role/capability hints, max attempts, timeout, and task metadata; edge editing supports success, failure, and approval paths; backend validation errors are shown inline before save/start.

## Notes

mac task task_f30cd1bd8b984851a4991d6d9ff44bab failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
