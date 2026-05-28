---
id: mac-azid
status: closed
deps: []
links: []
created: 2026-05-27T00:55:13Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-azid
---
# event_type_prefix LIKE-injection allows full-table dump with one wildcard

src/mac/services.py:3432-3434 — event_type_prefix is parameterized (SQL-injection safe) but unescaped % and _ are honored as LIKE wildcards. event_type_prefix=% returns the entire events table for any read-scope caller. Fix: escape LIKE wildcards in the input before binding.

## Close Reason

Closed
