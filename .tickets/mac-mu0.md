---
id: mac-mu0
status: closed
deps: []
links: []
created: 2026-05-24T09:46:24Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-mu0
---
# Expose first-class project import fields

MAC's ControlPlane.import_project_item already supports project, description, priority, dependencies, and metadata, but the API, mac CLI, and mac-hermes bridge only expose a narrower source/external/title/payload shape. Expose the full project import surface so project items can preserve project identity, dependency ordering, priority, and metadata across MAC and Hermes.

## Close Reason

Exposed project import project, description, priority, dependencies, and metadata fields through API, MAC CLI, and mac-hermes.
