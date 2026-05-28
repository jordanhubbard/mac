---
id: mac-1g3u
status: closed
deps: []
links: []
created: 2026-05-27T00:54:18Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-1g3u
---
# Tenant isolation is post-filter in Python via JSON labels, not a SQL constraint

src/mac/services.py:11280-11297, 10367 — _machine_allows_tenant is applied per-(agent,task) tuple AFTER the candidate set is built; the policy lives in machines.labels JSON. There is no DB-level constraint and no first-class tenant_pool table. A coding mistake that skips _agent_available_for (e.g., a future dispatch path) leaks work across tenants. Fix: model tenant pool as a first-class relation; enforce in dispatch SQL.

## Close Reason

Closed
