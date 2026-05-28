---
id: mac-5xwh
status: closed
deps: []
links: []
created: 2026-05-27T00:54:48Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-5xwh
---
# Worker bd comment <bead_id> permits argv smuggling from bd ready --json

src/mac/services.py:9085-9090 (and 9430, 9651) — bead_id is passed verbatim to bd comment/update --claim. _bead_issue_is_importable (services.py:8777) only checks non-empty + status=open, no regex. A hostile bd state with id='--help' or id='-c something' becomes a bd flag. Fix: validate bead_id format strictly (e.g., ^bd-[a-z0-9-]+$) and add '--' separator.

## Close Reason

Closed
