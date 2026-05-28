---
id: mac-a9a
status: closed
deps: []
links: []
created: 2026-05-24T09:42:19Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-a9a
---
# Submit Hermes local startup evidence to runtime proof

Hermes-side mac-hermes runtime-proof currently fetches hub-generated proof, which can reflect the hub startup environment rather than the calling Hermes agent's local runtime context and prompt bridge. Add a POST runtime-proof API path and make mac-hermes submit its local build_hermes_startup_report by default, with an escape hatch for hub-only proof, so the proof can verify the actual Hermes agent prompt/runtime object model.

## Close Reason

mac-hermes runtime-proof now submits local Hermes startup evidence to the hub proof endpoint by default.
