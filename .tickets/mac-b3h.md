---
id: mac-b3h
status: closed
deps: []
links: []
created: 2026-05-24T08:27:58Z
type: feature
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-b3h
---
# Materialize MAC task project bridge into Hermes runtime

Make MAC's task/project authority visible to Hermes agents at runtime by writing a Hermes-visible bootstrap contract during fleet deployment and exposing it in startup health, so Hermes can discover MAC work-context and lifecycle operations without out-of-band operator knowledge.

## Close Reason

Implemented Hermes-visible MAC task/project runtime context, deterministic Hermes identity binding for deployed workers, startup/UI reporting, docs, and tests.
