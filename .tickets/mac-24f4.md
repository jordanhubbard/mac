---
id: mac-24f4
status: closed
deps: []
links: []
created: 2026-05-27T00:53:50Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-24f4
---
# Rollout RESCUING has no exit path — one-way trap

src/mac/models.py:277-305 (ROLLOUT_ACTIONS): from RESCUING only rollback is allowed. No 'rescue complete → return to CANARYING/PLANNED' action; no service-side hook ties a successful rescue task back to the rollout. Succeeded rescue still requires manual rollback; a failed rescue task leaves the rollout stuck in RESCUING forever. evaluate_rollout_health while RESCUING just records events (rollout_service.py:371-384). Fix: add an explicit recover/complete-rescue transition with health re-eval.

## Close Reason

Closed
