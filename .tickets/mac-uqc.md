---
id: mac-uqc
status: closed
deps: []
links: []
created: 2026-05-25T19:15:08Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-uqc
---
# Require shared Qdrant and Firecrawl on every MAC agent

MAC fleet deployment must not allow agents to start without shared project memory and web search. Qdrant and Firecrawl must be mandatory true capabilities for every agent, and startup self-tests must fail readiness when either service is disabled or unreachable.

## Close Reason

Qdrant and Firecrawl are now mandatory for fleet deployments; startup self-tests report and fail on missing or unreachable shared memory/web search services.
