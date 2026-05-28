---
id: mac-7mwd
status: closed
deps: []
links: []
created: 2026-05-27T00:53:38Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-7mwd
---
# Eval-gate replay across rollouts sharing a version string

src/mac/rollout_service.py:285-298 — the latest-run lookup is keyed on (eval_set_id, target_kind=ROLLOUT_VERSION, target_id=rollout.version). rollouts.version is NOT unique (src/mac/store.py:757-772), so a passing eval_run for an OLD artifact gates a NEW rollout with the same version string but a different artifact_hash. Fix: include artifact_hash or rollout.id in the target_id.

## Close Reason

Closed
