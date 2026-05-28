---
id: mac-wcfy
status: closed
deps: []
links: []
created: 2026-05-27T00:53:17Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-wcfy
---
# Bind agent_id path param to bearer principal on heartbeat/claim-next/command-audit

src/mac/api.py:2776-2812 — /agents/{id}/heartbeat, /agents/{id}/claim-next, and /agents/{id}/command-audit use agent_id from the URL path with no check that the bearer principal is bound to that agent. Any agent (or write) token can heartbeat, claim, or audit-log as a peer. Forged command-audit rows mean the only forensic record of subprocess argv is worker-controlled. Fix: bind tokens to agent identity; reject path agent_id when it doesn't match the principal.

## Close Reason

Closed
