---
id: mac-p95
status: closed
deps: []
links: []
created: 2026-05-18T23:43:32Z
type: task
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-p95
---
# Implement mac worker registration dispatch and streaming AgentBus

Add the missing execution plane for mac: agents must register/heartbeat, claim work through a dispatch loop, run commands, and exchange high-speed content through an AgentBus-compatible streaming channel. This fills the gap observed after fleet deployment where rocky had 143 open tasks but no agents, claims, or leases.

## Acceptance Criteria

mac exposes and/or runs a worker agent registration + heartbeat loop; dispatch can claim eligible open tasks and renew/release leases; a durable AgentBus surface supports sending arbitrary content envelopes plus streaming ordered chunks between agents; CLI/API tests prove registration, dispatch, lease handling, bus publish/read, and streaming semantics; fleet deploy can start workers or documents the service path.

## Close Reason

Implemented worker self-registration/heartbeat/run loop integration and durable typed AgentBus streaming API/CLI with tests and deployment docs.
