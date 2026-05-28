---
id: mac-ztm
status: closed
deps: []
links: []
created: 2026-05-24T10:37:54Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-ztm
---
# Prove Hermes direct-session shell parity

Hermes runtime proof declares MAC API/CLI, hgmac, Beads, Git, quality gates, command audit, and web search, but it does not explicitly prove the generic shell execution and workspace file access that make a Hermes agent comparable to a Claude or Codex session working directly in the MAC repository. Add required shell_execution and workspace_file_access capabilities, verify them during startup, include them in runtime proof expectations, and document/test the direct-session parity workflow.

## Close Reason

Added required shell_execution and workspace_file_access runtime capabilities, startup probes for shell execution and transient workspace write/read access, proof expectation updates, direct-session workflow commands, docs, and tests.
