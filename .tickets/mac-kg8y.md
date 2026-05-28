---
id: mac-kg8y
status: closed
deps: []
links: []
created: 2026-05-27T00:53:52Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-kg8y
---
# Rollout PROMOTED does not deploy the artifact — environment/rollout drift

src/mac/deploy_service.py:264-330 (deploy_artifact) and src/mac/rollout_service.py advance_rollout are disjoint. Reaching PROMOTED does NOT call deploy_artifact. README's 'deploy atomically retires the prior active deployment' is true for deploy_artifact itself, but rollouts can advance to PROMOTED with the environment still on the old artifact. Fix: tie successful promote → environment deploy in the same flow, or document the manual step.

## Close Reason

Closed
