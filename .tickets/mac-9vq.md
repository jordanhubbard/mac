---
id: mac-9vq
status: closed
deps: []
links: []
created: 2026-05-20T16:06:14Z
type: task
priority: 1
mac-task-id: pending:mac-9vq
---
# Replace dict-shaped evidence verification with typed validators

Evidence verification currently depends on a large conditional validator over loosely shaped dictionaries. Each new evidence type adds special-case parsing and repeated repo/check gates, which makes review correctness hard to extend without regressions.

## Acceptance Criteria

Worker evidence manifests are parsed into typed models; evidence validators are registered by evidence_type; repo-anchor and check-result gates are reusable shared validators; unit tests cover each evidence type through the registry rather than copy-pasted branch tests.

## Notes

mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit
mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit
mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit
mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit
mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit
mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit
mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit
mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit
mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit
mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit
mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit
mac task task_c25e196d23204bd4b8c280e7344db2d7 failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
