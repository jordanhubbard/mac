---
id: mac-iaur
status: closed
deps: []
links: []
created: 2026-05-27T15:49:21Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-iaur
---
# Make dashboard project map and fleet views actionable

Projects view has confusing scoped counts, uneven create fields, hidden edit controls, and no token editability indicator. Map view draws topology without useful selection details and mixes operator-like agents with workers without explanation. Expose token capabilities in dashboard state, make project rows editable inline, clarify scoped metrics, and make map selection show useful typed details.

## Notes

User clarified fleet CRUD is the wrong model: fleets should be emergent from agent registration, not manually created/mutated here. Remove fleet create/edit/delete controls, add fleet identifiers to the agent UI, show durable vs derived project records, and keep read/write session visibility.

## Close Reason

Implemented dashboard session scope visibility, inline project table editing, actionable map selection details, read-only fleet view, agent fleet identifiers/create fields, and setup.sh new-hub deploy delegation.
