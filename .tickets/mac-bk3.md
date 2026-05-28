---
id: mac-bk3
status: closed
deps: []
links: []
created: 2026-05-24T08:58:05Z
type: feature
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-bk3
---
# Expose Hermes direct session capability contract

Why this issue exists: MAC/Hermes runtime coupling currently proves task/project authority and lifecycle operations, but Hermes still lacks an explicit direct-session parity contract that tells it which workspace, Beads workflow, hgmac agent-ops CLI, git/test commands, and web-search affordances make it equivalent to a Claude/Codex session working directly in the MAC repo. What needs to be done: extend the Hermes runtime context and startup/runtime proof to materialize and verify this session capability contract, render it in Hermes-facing Markdown, expose it in dashboard proof evidence, document it, and test the API/CLI/UI/Hermes-runtime path.

## Close Reason

Hermes runtime now carries a direct-session capability contract covering workspace, Beads, git/test workflow, hgmac agent ops, MAC CLIs, and Firecrawl search; startup/runtime proof/UI/docs/tests validate it.
