---
id: mac-k30g
status: closed
deps: []
links: []
created: 2026-05-27T00:52:56Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-k30g
---
# Bind /secrets/reveal accessor_agent_id to bearer principal

src/mac/api.py:3332-3337 — POST /secrets/{id}/reveal accepts accessor_agent_id from the request body and never compares it to the bearer-token principal. Any holder of a secret-scoped token (or any caller in open mode) can pass an arbitrary accessor_agent_id with a previously granted audit_id and redeem the handle; the secret_access_audit row records the spoofed agent_id, not the HTTP caller. Fix: derive accessor identity from the bearer principal; reject when body and principal disagree.

## Close Reason

Closed
