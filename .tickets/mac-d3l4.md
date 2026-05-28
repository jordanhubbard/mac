---
id: mac-d3l4
status: closed
deps: []
links: []
created: 2026-05-27T06:53:36Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-d3l4
---
# Fix project CRUD controls in dashboard

Project CRUD in the dashboard renders update/delete as oversized form controls and the delete action does not work from the browser. Make edit a disclosure control, make delete a normal button, and verify the UI dispatches a working DELETE request.

## Close Reason

Fixed project CRUD controls: edit is a disclosure, delete is a direct button, project focus uses a separate data attribute so form/button clicks no longer trigger a re-render, and tests pass.
