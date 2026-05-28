---
id: mac-s0j
status: closed
deps: []
links: []
created: 2026-05-19T04:54:42Z
type: task
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-s0j
---
# Add telemetry gates for safe worker canaries

Before enabling mac-agent loop mode, add coherent telemetry for worker routing decisions, dry-run claim candidates, claim attempts, executor lifecycle, AgentBus publication/read activity, and deploy-time verification surfaces. The goal is to prove canary behavior without consuming migrated ACC work blindly.

## Close Reason

Telemetry gates deployed and verified on rocky, natasha, and bullwinkle; dry-run claims are observable and inert with canary requirement enabled.
