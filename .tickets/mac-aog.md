---
id: mac-aog
status: closed
deps: []
links: []
created: 2026-05-24T08:42:41Z
type: feature
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-aog
---
# Inject MAC task project context into Hermes prompts

Make deployed Hermes agent sessions actually ingest MAC's task/project runtime bridge by patching upstream Hermes prompt construction and ensuring mac/mac-hermes commands are on the Hermes runtime PATH.

## Close Reason

Added MAC-managed Hermes prompt-builder patch, PATH exposure for mac/mac-hermes, startup/UI prompt-bridge evidence, docs, and tests.
