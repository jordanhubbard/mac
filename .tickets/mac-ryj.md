---
id: mac-ryj
status: closed
deps: []
links: []
created: 2026-05-21T09:02:28Z
type: task
priority: 1
mac-task-id: pending:mac-ryj
---
# Automate disk hygiene for MAC deploy and ACC replacement artifacts

Rocky filled its root filesystem after MAC replacement because old ACC Hermes releases, ACC build/deploy/log artifacts, ACC source build outputs, AgentFS review scratch, and unbounded MAC deploy backups accumulated. Add automated retention/cleanup for generated MAC deploy backups and clearly obsolete ACC replacement artifacts, while preserving migrated ACC data and live MAC/Hermes state. Include operator telemetry before/after cleanup so future disk pressure is visible before deploys fail.

## Notes

mac task task_2e3012690dc44ce38c4776bdd46aff03 failed: beads_failed_task_retry_limit | evidence: ev_624d5384650847a7bf06f72feb435bec
mac task task_2e3012690dc44ce38c4776bdd46aff03 failed: beads_failed_task_retry_limit | evidence: ev_624d5384650847a7bf06f72feb435bec
mac task task_2e3012690dc44ce38c4776bdd46aff03 failed: beads_failed_task_retry_limit | evidence: ev_624d5384650847a7bf06f72feb435bec
mac task task_2e3012690dc44ce38c4776bdd46aff03 failed: beads_failed_task_retry_limit | evidence: ev_624d5384650847a7bf06f72feb435bec
mac task task_2e3012690dc44ce38c4776bdd46aff03 failed: beads_failed_task_retry_limit | evidence: ev_624d5384650847a7bf06f72feb435bec
mac task task_2e3012690dc44ce38c4776bdd46aff03 failed: beads_failed_task_retry_limit | evidence: ev_624d5384650847a7bf06f72feb435bec

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
