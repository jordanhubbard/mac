---
id: mac-3qe
status: closed
deps: []
links: []
created: 2026-05-23T18:57:19Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-3qe
---
# Track explicit review claims for bead-backed task lifecycles

The end-to-end bead lifecycle needs a distinct reviewer claim event with enough metadata to audit the review work: reviewer agent, reviewed task, project, executor evidence, repository worktree/head/ref, checks, and work summary. The worker review nudge path should claim the review before doing review work, record the metadata on the task, mirror it to the bead ledger, and expose it through the API.

## Close Reason

Implemented explicit review claim metadata/API/worker flow with Beads ledger and notifier coverage
