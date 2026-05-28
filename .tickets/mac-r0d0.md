---
id: mac-r0d0
status: closed
deps: []
links: []
created: 2026-05-27T00:53:14Z
type: bug
priority: 0
mac-task-id: pending:mac-r0d0
---
# Reviewer-independence uses unauthenticated evidence.created_by string

src/mac/review_service.py:332-369 — agent_is_current_owner_or_latest_evidence_author and latest_executor_evidence_author both read evidence.created_by, a TEXT column set from caller-supplied parameter at services.py:4531. There is no proof at evidence-insert time that the caller actually is created_by. Combined with the unbound principal on /tasks/{id}/evidence, a worker can submit evidence with created_by=other-agent and then 'review' its own work. Fix: derive created_by from the authenticated principal at insert (see also the principal-binding bug).

## Close Reason

Resolved by mac-rreh: /tasks/{id}/evidence now binds created_by to the bearer principal via TokenPrincipal.assert_actor. agent_is_current_owner_or_latest_evidence_author + latest_executor_evidence_author still read evidence.created_by, but that string is now derived from authenticated identity at insert time, not a payload self-assertion. Reviewer-independence is now enforceable.
