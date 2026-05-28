---
id: mac-tk1b
status: closed
deps: []
links: []
created: 2026-05-27T00:53:44Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-tk1b
---
# Eval-run records have no signer/auth binding — anyone can mint passing runs

src/mac/eval_service.py:171-248 — record_eval_run accepts target_id as a free string with created_by self-asserted. No signer check, no auth, no evidence-to-target binding (evidence only checked for kind=='eval' at 191-194). Any caller can mint passing runs that satisfy promotion gates. Fix: bind created_by to the authenticated principal; require signed eval evidence.

## Close Reason

Closed
