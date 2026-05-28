---
id: mac-uwjo
status: closed
deps: []
links: []
created: 2026-05-27T05:24:37Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-uwjo
---
# Ensure new hub deployment creates a fleet record

Brand-new deployments with one configured hub agent currently show zero fleets in the control plane/UI. After a new hub node is created, the control plane should contain at least one fleet record for that deployment.

## Close Reason

Implemented automatic fleet record bootstrap for new hub deployments and worker registration; verified with full pytest suite.
