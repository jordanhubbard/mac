---
id: mac-7osn
status: closed
deps: []
links: []
created: 2026-05-27T00:54:56Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-7osn
---
# /events view leaks command_audit.argv unredacted

src/mac/store.py:1019, 399-417 — the unified /events projection emits json(argv) from command_audit unredacted. Any CLI invocation that put a secret on the command line (--token=...) becomes plaintext in /events JSON. /events is GET so it only requires read scope (src/mac/api.py:896-897). Fix: redact argv in the events view or apply secret-pattern scrubber server-side.

## Close Reason

Closed
