---
id: mac-5u1f
status: closed
deps: []
links: []
created: 2026-05-27T00:53:11Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-5u1f
---
# submit_review accepts any task evidence as approval verdict

src/mac/review_service.py:130-164 — ReviewService.submit_review only checks evidence.task_id == review.task_id. It does not require the evidence to be a signed review_verdict, signed by the reviewer, or even authored by the reviewer. The signed-verdict identity gates live in services.py:_find_review_verdict_evidence but are only consulted from _validate_publication_evidence. Direct callers of submit_review (REST API, CLI) can mark a review APPROVED with the executor's own evidence row. Fix: enforce the verdict requirements inside submit_review itself, not only at publication time.

## Close Reason

Closed
