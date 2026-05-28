---
id: mac-5y9
status: closed
deps: []
links: []
created: 2026-05-24T10:23:55Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-5y9
---
# Expose first-class project API and CLI

Projects are first-class in dashboard summaries and Hermes work-context, but MAC lacks a general project list/detail API and operator CLI surface comparable to tasks and agents. Add ControlPlane project summary/detail access, expose /projects and /projects/{project}, add mac project CLI commands, and include the project API/CLI in Hermes proof contracts so projects are directly addressable across MAC API, CLI, UI, and Hermes.

## Close Reason

Implemented first-class project list/detail API, MAC and Hermes CLI commands, runtime proof contract updates, docs, and tests.
