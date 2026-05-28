---
id: mac-5cs
status: closed
deps: []
links: []
created: 2026-05-18T22:26:59Z
type: task
priority: 3
assignee: agent_natasha
mac-task-id: pending:mac-5cs
---
# Reconcile fleet deploy after SSH transport loss

During the hardened mac redeploy, Bullwinkle completed the remote deployment and wrote a healthy post manifest, but the local SSH client still exited 255 with 'Connection reset by peer' after completion. Add a deploy-side reconciliation step that, on SSH transport failure, reconnects to the host, checks the expected deploy log/post manifest/service health for that timestamp, and reports success if the remote deployment actually completed.

## Acceptance Criteria

A transient SSH disconnect after remote deploy completion does not make deploy/deploy-mac-fleet.sh fail; the script reconnects and verifies post manifest, health, and service state before deciding success or failure.

## Notes

mac task task_b71b688e54364d129e2d4c13fbbfd247 failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
