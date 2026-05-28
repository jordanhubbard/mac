---
id: mac-31s
status: closed
deps: []
links: []
created: 2026-05-24T09:12:31Z
type: feature
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-31s
---
# Verify Hermes direct-session capability availability

Why this issue exists: Hermes runtime context now declares direct-session parity with a Codex/Claude shell, but startup/runtime proof only checks that the capability names are present. That leaves a gap: Hermes may understand the contract yet lack runnable mac/mac-hermes/hgmac/bd/git commands, workspace/project contract files, or web-search configuration. What needs to be done: extend Hermes startup/runtime proof to validate direct-session capability availability from the runtime context, including command/path resolution, workspace/project contract presence, quality-gate command resolvability, and Firecrawl environment configuration; expose the results through runtime proof and dashboard evidence; add focused tests.

## Close Reason

Hermes startup/runtime proof now validates direct-session capability availability: command/path resolution, workspace and project contract presence, project toolchain, quality gate, and Firecrawl environment.
