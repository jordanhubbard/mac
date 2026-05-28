---
id: mac-4pkm
status: closed
deps: []
links: []
created: 2026-05-27T00:54:21Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-4pkm
---
# MessagingService.deliver_messages is not transactional — double-delivery race

src/mac/messaging_service.py:169-189 — SELECT-then-UPDATE without BEGIN IMMEDIATE. Two concurrent deliver_messages calls to the same recipient can both see the queued row and both UPDATE it, double-delivering. Status flips to 'delivered' before HTTP response is sent, so client disconnect mid-response drops messages (at-most-once on the wire, exactly-once in DB — worst combo). Fix: claim row inside transaction; mark delivered only after client ack.

## Close Reason

Closed
