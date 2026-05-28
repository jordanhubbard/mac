---
id: mac-v7p
status: closed
deps: []
links: []
created: 2026-05-18T18:37:24Z
type: task
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-v7p
---
# Add Hermes state and Slack startup checks

Wire mac startup to inventory Hermes soul/memory/state references and detect whether upstream Hermes needs the slack_accounts.json activation shim before Slack can start from account-file-only configuration.

## Close Reason

Implemented Hermes startup state inventory and upstream Slack account-file shim handling with tests and docs.
