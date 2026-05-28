---
id: mac-hjw
status: closed
deps: []
links: []
created: 2026-05-24T10:00:13Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-hjw
---
# Expose Hermes claim-next bridge operation

Hermes can inspect MAC tasks and claim specific tasks through mac-hermes, but the Hermes-facing bridge does not expose the dispatch-aware claim-next operation that lets an agent accept the next eligible MAC task using the same policy as worker/Codex style sessions. Add claim-next to the Hermes adapter CLI, operation/runtime/proof contracts, docs, and tests so tasks remain first-class operational objects for Hermes agents.

## Close Reason

Added mac-hermes claim-next bridge support, runtime/proof contract coverage, docs, and regression tests.
