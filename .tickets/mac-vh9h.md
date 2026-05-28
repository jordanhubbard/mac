---
id: mac-vh9h
status: closed
deps: []
links: []
created: 2026-05-27T00:53:47Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-vh9h
---
# Verify-artifact while PAUSED can swap artifact_uri/hash; resume does not re-gate

src/mac/rollout_service.py:202-249 — verify_rollout_artifact lets a PAUSED rollout overwrite artifact_uri and artifact_hash; the subsequent advance_rollout('resume') does NOT re-run _install_ready. Combined with the eval-replay bug, an attacker who can pause+swap can ride a previously-passing eval run. Fix: on artifact swap, force re-gating before resume.

## Close Reason

Closed
