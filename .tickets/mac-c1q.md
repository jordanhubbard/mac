---
id: mac-c1q
status: closed
deps: [mac-v2i]
links: []
created: 2026-05-20T04:49:29Z
type: task
priority: 0
mac-task-id: pending:mac-c1q
---
# Test: same-role agents must not be able to approve each other (collusion guard)

No test pins that the reviewer agent and the executor agent cannot share a role/persona. Setup: two agents souled to the same persona slug (e.g., both code-reviewer), executor produces a verified manifest, advance_default_review_workflow runs. Assert: reviewer selection refuses the peer; task remains in NEEDS_REVIEW until an agent with a different role becomes available. Without this test the collusion vector in issue 5 has no regression coverage.

## Close Reason

Closed
