---
id: mac-kgi5
status: closed
deps: []
links: []
created: 2026-05-27T00:53:12Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-kgi5
---
# Bind AgentBus/messaging sender_agent_id to bearer principal

src/mac/api.py:3135-3190 — /agentbus/publish, /agentbus/streams (append/close), and /messages take sender_agent_id from the request body/query and never compare it to the bearer principal. With one shared auth_tokens map (api.py:880-888), any agent-scoped token can publish, append, close streams, or send messages AS any other agent. Fix: derive sender identity from the principal; reject mismatches.

## Close Reason

Closed
