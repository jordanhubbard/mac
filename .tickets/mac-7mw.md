---
id: mac-7mw
status: closed
deps: []
links: []
created: 2026-05-24T09:34:18Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-7mw
---
# Validate Hermes runtime first-class object model

The runtime proof now exposes a task/project/agent object matrix, but Hermes' deployed runtime context and startup verifier do not yet carry or validate a structured object model. Add first_class_objects to the Hermes runtime context JSON and Markdown, validate it in startup health, surface it in runtime proof, docs, and tests so Hermes' prompt/runtime understanding explicitly matches MAC's task, project, and agent objects.

## Close Reason

Added and validated the Hermes runtime first-class object model for MAC tasks, projects, and agents.
