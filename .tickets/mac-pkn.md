---
id: mac-pkn
status: closed
deps: []
links: []
created: 2026-05-25T23:10:36Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-pkn
---
# Make Beads bridge repair idempotent for existing DBs

The live c26 bridge repair endpoint fails at bd bootstrap when the Beads database already exists, before it can export .beads/issues.jsonl and clear authority drift. Repair should tolerate already-bootstrapped repositories and proceed to dolt pull, ready, export, and poll-after reconciliation.

## Close Reason

Repair path now skips existing embedded DBs, upgrades stale bd binaries during deploy, and persists repaired tracked exports through git push.
