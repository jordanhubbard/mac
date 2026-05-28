---
id: mac-jmjc
status: closed
deps: []
links: []
created: 2026-05-27T00:53:41Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-jmjc
---
# Rollout health gate trivially bypassable with empty required_checks

src/mac/rollout_service.py:501-505 — if health_policy.required_checks is empty/unset, the gate falls back to 'whatever keys the caller supplied'. Calling evaluate_rollout_health(rollout_id, {}) yields required=[], failed=[], status='healthy' → the promotion gate at lines 256-262 passes. Fix: default policy must require an explicit, non-empty allowlist; reject empty checks at policy create.

## Close Reason

Closed
