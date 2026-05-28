---
id: mac-yow
status: closed
deps: []
links: []
created: 2026-05-24T10:54:47Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-yow
---
# Validate Hermes prompt runtime markdown contract

Startup proof verifies the MAC runtime JSON contract and prompt-builder loader, while deploy verifies the rendered mac-runtime-context.md content. Move that first-class prompt-content validation into startup health: summarize required markdown snippets, fail required runtimes when mac-runtime-context.md is missing first-class task/project/agent, direct-session, or bridge commands, and include shell_execution/workspace_file_access in the lower runtime-context capability contract check.

## Close Reason

Validated Hermes runtime markdown contract in startup health, API runtime proof, deploy checks, docs, and tests.
