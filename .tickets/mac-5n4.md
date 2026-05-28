---
id: mac-5n4
status: closed
deps: []
links: []
created: 2026-05-20T08:13:50Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-5n4
---
# Fix mac repository contract test PATH

Backfill verification showed the canonical test command needs .venv/bin on PATH so process E2E tests can find installed console scripts such as mac-agent after project bootstrap.

## Close Reason

Backfill verification showed process E2E needs .venv/bin on PATH; updated mac's repository contract, docs, and tests to use the corrected canonical test command.
