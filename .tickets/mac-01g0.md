---
id: mac-01g0
status: closed
deps: []
links: []
created: 2026-05-27T00:53:01Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-01g0
---
# Secret routes never call principal.assert_tenant() — cross-tenant secret access

src/mac/api.py:3315-3341 — None of the four /secrets/* routes invoke principal.assert_tenant(). A token bound to tenant A can list/create/access/reveal secrets scoped to tenant B; the only check is service-layer agent-vs-machine scope match in secrets_service.py:263-284 using caller-supplied accessor_agent_id. Fix: add tenant assertion to every /secrets/* route, mirroring /environments and /hermes-instances.

## Close Reason

Closed
