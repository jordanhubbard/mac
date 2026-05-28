---
id: mac-nml9
status: closed
deps: []
links: []
created: 2026-05-27T05:51:11Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-nml9
---
# Load fleet-scoped hgmac tokens from ~/.mac/.env

After fleet-scoped tokens were added to ~/.mac/.env, hgmac can still fail with missing bearer token unless the caller manually sources the env file. The CLI should load the home-scoped env file as a fallback so hgmac --fleet rocky resolves MAC_API_TOKEN__ROCKY.

## Close Reason

hgmac now loads fleet-scoped tokens from ~/.mac/.env as fallback; verified rocky command works without sourcing and full pytest suite passes.
