---
id: mac-t6z
status: closed
deps: []
links: []
created: 2026-05-23T20:48:55Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-t6z
---
# Allow signed rejected review verdicts to complete reviews

Default review verdict validation currently applies approval publication requirements to rejected verdicts. A reviewer can sign a coherent rejection with blockers, but the workflow rejects that verdict because repo.dirty=false and pushed=true are not satisfied, causing repeated reviewer nudges instead of marking the review rejected. Accept signed rejected review_verdict evidence after identity, reviewed_evidence_id, and verdict-shape checks, then submit the review as rejected without publication.

## Close Reason

Fixed. Rejected review_verdict evidence now only needs a valid signed rejection tied to the executor evidence and a worktree digest; approval-only clean/pushed repo checks remain required for approved verdicts. Added regression coverage and verified with full pytest.
