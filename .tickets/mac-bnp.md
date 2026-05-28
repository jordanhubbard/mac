---
id: mac-bnp
status: closed
deps: []
links: []
created: 2026-05-19T01:54:25Z
type: task
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-bnp
---
# Harden AgentBus stream contracts

Commit the pending AgentBus hardening changes: require directed streams, validate stream IDs, cap chunk payloads, authorize event readers before streaming, and clamp event polling parameters.

## Acceptance Criteria

AgentBus streams require recipients; invalid stream IDs and oversized chunks are rejected; unauthorized readers receive HTTP 403 before streaming starts; full pytest suite passes.

## Close Reason

Done
