---
id: mac-g6g
status: closed
deps: []
links: []
created: 2026-05-21T05:41:47Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-g6g
---
# Harden evidence contracts and require GitHub CLI on agents

Agents are producing semantically valid execution and review evidence, but MAC's validators only accept one narrow shape, causing good work to fail and review nudges to loop. Broaden evidence normalization for tests/checks, align review verdict prompts with the verifier, and require/install gh on every agent so workers can open PRs instead of leaving pushed branches stranded.

## Close Reason

Broadened pass/check evidence normalization, aligned reviewer prompts with review verdict verifier requirements, and made gh a managed worker requirement installed by fleet deploy.
