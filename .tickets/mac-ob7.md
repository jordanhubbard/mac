---
id: mac-ob7
status: closed
deps: []
links: []
created: 2026-05-24T11:11:27Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-ob7
---
# Require Hermes oneshot executor in runtime proof

Direct-session parity should include the production executor path that lets a Hermes-backed MAC agent do task work like this Codex session. Add mac-hermes-task-executor as a required Hermes runtime session capability, include it in rendered prompt/direct workflow/deploy prompt verification, startup health checks, API runtime proof evidence, docs, and tests.

## Close Reason

Required mac-hermes-task-executor as a Hermes direct-session runtime capability, surfaced it in startup/runtime proof, deploy prompt verification, env example, docs, and tests.
