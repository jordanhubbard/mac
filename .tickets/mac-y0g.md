---
id: mac-y0g
status: closed
deps: [mac-dyk]
links: []
created: 2026-05-20T04:49:23Z
type: task
priority: 0
mac-task-id: pending:mac-y0g
---
# Test: cross-tenant auto-review must refuse — pin the tenancy boundary

No test today proves that tenant A's reviewable task cannot be auto-approved by tenant B's idle agent. With the cross-tenant leak (issue 3) this is required regression coverage. Test setup: register two tenants with one agent each (bound to their tenant via hermes_instance_id), submit a task as tenant A, call advance_default_review_workflows (or /reviews/default/tick), assert tenant B's agent was NOT selected as reviewer. Should pass after the tenancy filter ships and fail before.

## Close Reason

Closed
