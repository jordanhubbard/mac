---
id: mac-zipf
status: closed
deps: []
links: []
created: 2026-05-27T00:54:23Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-zipf
---
# Notifier delivery is two non-atomic statements with no idempotency key

src/mac/notifier_service.py:184-211 — _send_message inserts a message row; _mark_notification updates state in a separate statement. A crash between them re-runs delivery and duplicates the downstream message. messages table has no idempotency key. Fix: combine into one transaction; add idempotency key column.

## Close Reason

Closed
