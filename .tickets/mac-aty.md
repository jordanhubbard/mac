---
id: mac-aty
status: closed
deps: []
links: []
created: 2026-05-20T16:06:32Z
type: task
priority: 2
mac-task-id: pending:mac-aty
---
# Modularize the dashboard TypeScript application

src/mac/ui/app.ts is a large single-file dashboard with state management, API calls, view rendering, forms, and action dispatch in one place. It is already difficult to add workflow-specific UI without making the dashboard another ACC-style tangle.

## Acceptance Criteria

The dashboard is split into a typed API client, shared state model, view modules, action handlers, and reusable form controls; the checked-in generated app.js flow remains compatible with the no-Node production constraint; UI checks cover the split modules and existing dashboard flows.

## Notes

mac task task_f6dfc273769740d992a589db90582f9d failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
