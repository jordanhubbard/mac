---
id: mac-rreh
status: closed
deps: []
links: []
created: 2026-05-27T00:53:07Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-rreh
---
# Bind evidence.created_by to bearer principal (evidence forgery)

src/mac/api.py:2251-2259, src/mac/services.py:4502 — POST /tasks/{task_id}/evidence has no principal dependency and add_evidence writes created_by verbatim from payload. Any write-scoped token can post mac.worker_evidence.v1 evidence claiming any agent_id; combined with the signature gap (#signed_by-not-in-MAC), one token can forge both executor and reviewer evidence under different created_by values, defeating reviewer-independence. Fix: derive created_by from the authenticated principal at the service boundary.

## Close Reason

Closed
