---
id: mac-wfct
status: closed
deps: []
links: []
created: 2026-05-27T00:53:53Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-wfct
---
# Eval gate only checked on promote, not on start_canary

src/mac/rollout_service.py:275-308 — eval gate fires only when action == 'promote'. INSTANT strategy and start_canary skip the gate entirely even when required_eval_set_id is set. Half a gate. Fix: consult the eval gate on every state-advance into CANARYING/PROMOTED when required_eval_set_id is set.

## Close Reason

Closed
