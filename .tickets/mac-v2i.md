---
id: mac-v2i
status: closed
deps: []
links: []
created: 2026-05-20T04:48:58Z
type: bug
priority: 0
mac-task-id: pending:mac-v2i
---
# Auto-review: same-persona/same-role agents can approve each other (collusion vector)

services.py:2453 _select_default_reviewer blocks the executor itself via agent_has_owned_task but does not block peers from the same persona or role family. A workflow where two code-reviewer-souled agents take turns approving each other is allowed today. In an autonomous swarm this is an obvious collusion path — there is no human to catch agents endorsing their own kind. Fix: the reviewer's role slug must differ from the executor's role slug (e.g., code-reviewer can't approve code-reviewer work — needs QA or eng-manager). Alternatively: track the executor's persona on the task and refuse a reviewer with the same persona.id. Pick one; the current behavior trusts role homogeneity.

## Close Reason

Closed
