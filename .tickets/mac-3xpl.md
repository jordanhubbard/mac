---
id: mac-3xpl
status: closed
deps: []
links: []
created: 2026-05-27T00:55:04Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-3xpl
---
# bd ready --json import has no schema validation or length cap

src/mac/services.py:8504-8830 — title/description from bd ready --json flow straight into MAC project items. Newlines, control chars, ANSI escapes, arbitrary length all accepted. Will appear unsanitized in any TUI/log; metadata payload is unbounded. Fix: validate schema; cap lengths; strip control chars and ANSI.

## Close Reason

Closed
