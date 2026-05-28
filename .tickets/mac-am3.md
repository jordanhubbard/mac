---
id: mac-am3
status: closed
deps: []
links: []
created: 2026-05-22T17:55:36Z
type: task
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-am3
---
# Push dashboard changes to fleet

Deploy the latest main branch MAC dashboard/control-plane changes to the configured fleet after the code has been pushed to origin.

## Acceptance Criteria

Fleet deploy command completes successfully for the selected hub fleet; deployed hosts report the expected git revision or service status after deploy; Beads and git are clean and pushed after the operation.

## Notes

Rocky fleet deploy exposed two deployment issues: (1) Qdrant systemd units used stale /etc/mac/qdrant.env instead of the fleet-specific env file; (2) local MAC_DEPLOY_DRAIN_MODE/TIMEOUT/POLL overrides were not passed to the remote deploy script, and Rocky's large DB made the 10s drain API helper timeout. Patched both and updated deploy tests before retrying.

## Close Reason

Deployed commit 9499aaf05fbd64b0f6af3a39a795d82a381f557c to rocky, natasha, and bullwinkle. Fixed deploy blockers for fleet-specific Qdrant systemd env rendering, remote drain override propagation, and Rocky hub URL reachability over Tailscale; verified all three hosts healthy and registered.
