---
id: mac-gw9
status: closed
deps: []
links: []
created: 2026-05-25T06:27:11Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-gw9
---
# Handle exhausted review/revision loops in project e2e workflows

The c26 project e2e now creates tasks, executes the epic, approves and publishes it, unblocks the dependent review/revision task, and has agents execute/review that task. After multiple rejected review cycles the dependent task remains needs_review and default review reports waiting_for_reviewer instead of reaching a clear terminal state, provisioning a fresh eligible reviewer, or surfacing an operator-actionable blocker. Add workflow handling and regression coverage for rejected review loops and reviewer exhaustion.

## Close Reason

Fixed default review retry deadlock by allowing prior owners to review newer evidence while still blocking latest evidence self-review, and by failing exhausted rejected tasks; covered by regression tests.
