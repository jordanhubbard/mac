---
id: mac-oxmo
status: closed
deps: []
links: []
created: 2026-05-27T06:18:25Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-oxmo
---
# Prompt for dashboard API token on first auth failure

The Rocky dashboard loads the public UI shell but data requests fail with 'Dashboard data needs a token with read scope.' On first connection, the UI should actively request/focus API token entry and pass it through on refresh instead of leaving the user at a passive empty dashboard.

## Close Reason

Dashboard now opens/focuses token prompt when no token is present or a data request returns 403; verified with dashboard tests and full pytest suite.
