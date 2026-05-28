---
id: mac-q38
status: closed
deps: []
links: []
created: 2026-05-20T04:49:59Z
type: task
priority: 0
mac-task-id: pending:mac-q38
---
# Auto-review: evidence-type taxonomy is bloated and aliased — pick one synonym per axis

services.py:2367 the verification taxonomy accepts repo_change|code|git as synonyms for repo evidence; documentation|investigation|decision_record as synonyms for docs; status accepts complete|verified|pass|passed. Every unverified alias is a separate door that future workflow logic must remember to validate consistently. Pick one canonical synonym per axis, document the schema in a single place (docs/agent-roles.md or docs/evidence-manifest.md), and reject the others at the boundary. This is a maintainability and correctness multiplier for issue 2.

## Close Reason

Closed
