---
id: mac-a2e
status: closed
deps: []
links: []
created: 2026-05-20T20:22:27Z
type: task
priority: 1
assignee: agent_natasha
mac-task-id: pending:mac-a2e
---
# Make launchd worker shutdown requeue active leases

Stopping Bullwinkle's launchd mac-agent during deploy did not clear its active lease; the operator had to post an offline heartbeat manually. The macOS service wrapper or deploy drain path should guarantee active leases are released/requeued on controlled shutdown just like the systemd workers did.

## Notes

mac task task_02458acbc9664da4b1d774586f90e079 failed: beads_failed_task_retry_limit
mac task task_02458acbc9664da4b1d774586f90e079 failed: beads_failed_task_retry_limit
mac task task_02458acbc9664da4b1d774586f90e079 failed: beads_failed_task_retry_limit
mac task task_02458acbc9664da4b1d774586f90e079 failed: beads_failed_task_retry_limit
mac task task_02458acbc9664da4b1d774586f90e079 failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
