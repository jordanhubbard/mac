---
id: mac-p5a4
status: closed
deps: []
links: []
created: 2026-05-27T00:53:24Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-p5a4
---
# submit_review writes status + history in separate non-transactional statements

src/mac/review_service.py:157-172 — bare store.execute calls outside any transaction() block. A crash between them leaves the review APPROVED without a task.review_completed history row, weakening audit. Other methods (publish_task, request_review) correctly use 'with self.store.transaction()'. Fix: wrap both writes in one transaction.

## Close Reason

Closed
