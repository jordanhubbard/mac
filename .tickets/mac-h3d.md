---
id: mac-h3d
status: closed
deps: []
links: []
created: 2026-05-20T04:49:10Z
type: bug
priority: 0
mac-task-id: pending:mac-h3d
---
# Worker: _assignment_is_current swallows every exception

worker.py:343 _assignment_is_current uses 'except Exception: return True'. The comment says 'preserve old behavior on network blip' but that catch also eats TypeError/KeyError from a malformed API response, allowing the worker to proceed with completing a task it doesn't own. In an autonomous loop a worker that self-corrupts the ledger has no human to notice. Narrow to MacApiError (the network/API exception class) and let programming errors bubble. Add a regression test that an unexpected exception type makes the assignment check return False (or raise), not silently True.

## Close Reason

Closed
