---
id: mac-x1e
status: closed
deps: []
links: []
created: 2026-05-25T20:45:15Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-x1e
---
# Require Qdrant and Firecrawl in agent startup self-test

Agents must always have shared Qdrant memory and Firecrawl web search enabled. Make those flags mandatory true in fleet configuration/deploy metadata and fail agent startup self-tests when either service is disabled or unreachable.

## Close Reason

Made Qdrant shared memory and Firecrawl web search mandatory in deploy validation, fleet setup metadata, Hermes startup reporting, and generated agent startup self-tests; added regression coverage.
