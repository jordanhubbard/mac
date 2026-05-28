---
id: mac-xqn6.2
status: deferred
deps: []
links: []
created: 2026-05-26T17:39:39Z
type: feature
priority: 2
parent: mac-xqn6
mac-task-id: pending:mac-xqn6.2
---
# Bootstrap missing project contract through explicit registration flow

Add an explicit opt-in path for MAC to create a default .mac/project.yaml when a repository is registered without one. The generated contract must be committed and pushed by the registering hub/agent identity before the project becomes eligible for import.

## Acceptance Criteria

Default contract creation is never silent; API/CLI/Hermes expose an explicit bootstrap flag or command; generated contract includes project, platforms, toolchain, bootstrap, test, and evidence sections; MAC commits and pushes the file; failure to push leaves the repository unhealthy and imports no tasks.
