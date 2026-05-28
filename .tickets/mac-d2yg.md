---
id: mac-d2yg
status: closed
deps: []
links: []
created: 2026-05-27T07:39:33Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-d2yg
---
# Apply CRUD disclosure/delete pattern across dashboard

Fleet, agent, and task CRUD controls still use oversized update/delete form blocks. Follow the project CRUD pattern: edit as disclosure, delete as a direct button, avoid click-handler collisions, cache-bust the dashboard bundle, and verify delete dispatch still works.

## Notes

Generalized project CRUD UI pattern to fleets, agents, and tasks: edit is a disclosure, delete is a direct button handled by shared runDirectDelete, and stale submit-form delete actions were removed from runAction.

## Close Reason

Fixed: fleet, agent, and task CRUD controls now use edit disclosures and direct delete buttons, with shared direct-delete event handling and cache-busted UI assets. Tests pass.
