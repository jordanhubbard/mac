---
id: mac-bobn
status: in_progress
deps: []
links: []
created: 2026-05-26T17:56:24Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-bobn
---
# Make macOS Hermes services resilient to fd exhaustion and stale mesh

Bullwinkle stopped responding to Slack because Tailscale was stopped and the Hermes gateway was wedged at the launchd soft maxfiles limit of 256 with repeated kanban.db handles. The gateway state still reported Slack connected with a stale timestamp, and MAC only showed the agent stale after heartbeat stopped. Make the deployment and health checks catch and prevent this class of failure.

## Acceptance Criteria

macOS service wrappers raise the soft file descriptor limit before launching Hermes/MAC workers; startup or liveness checks detect stopped Tailscale/mesh connectivity to the hub; stale Slack gateway state or fd exhaustion is reported as unhealthy instead of connected; tests cover wrapper generation and health reporting.

## Notes

Applied live Bullwinkle remediation on 2026-05-26: restarted Tailscale, raised service wrapper fd limit to 4096 on Bullwinkle, restarted Hermes gateway and MAC worker. Durable deploy-template fd-limit fix is in progress; remaining work is explicit mesh/gateway liveness detection.
