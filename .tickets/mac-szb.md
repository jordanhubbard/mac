---
id: mac-szb
status: closed
deps: []
links: []
created: 2026-05-23T20:58:49Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-szb
---
# Requeue rejected review tasks instead of leaving ownerless running tasks

When a pending review is rejected or changes are requested, ReviewService transitions the task from reviewing to running. The original executor lease was released when the task entered needs_review, so this leaves state=running with no owner_agent_id and no lease, which dispatch cannot claim. Rejected review work should return to open for another executor attempt.

## Close Reason

Fixed. Review rejection and changes_requested now transition tasks from reviewing back to open, not ownerless running; regression coverage verifies the rejected verdict requeues with no owner or lease. Full pytest passes.
