---
id: mac-xqn6.5
status: closed
deps: []
links: []
created: 2026-05-26T17:39:45Z
type: bug
priority: 1
parent: mac-xqn6
mac-task-id: pending:mac-xqn6.5
---
# Block dispatch and Beads polling for unhealthy project registrations

If a project preflight fails, MAC should not import ready Beads into runnable tasks or allow dispatch. The user should see the problem as a project health issue rather than discovering it after agents create failed evidence or endless review nudges.

## Acceptance Criteria

Unhealthy repositories produce zero new runnable tasks; existing tasks from that repository are paused or blocked with a clear reason; dashboard/API/Hermes/Slack report the human project name and failing checks; repair/poll can retry after remediation; tests cover failed preflight preventing import and dispatch.

## Close Reason

Dispatch now skips OPEN tasks whose project name appears in any beads_repository with metadata.health.state=='unhealthy'. The tasks remain in the ledger (visible to operators) but cannot be claimed until preflight passes (mac-xqn6.1 fixes the contract gate; bootstrap/git-access pieces are mac-xqn6.2/.4). Added _unhealthy_beads_projects() helper used by _dispatch_ordered_tasks.
