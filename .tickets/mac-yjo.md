---
id: mac-yjo
status: closed
deps: []
links: []
created: 2026-05-23T19:19:47Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-yjo
---
# Forward notifier updates to Hermes Slack home channels

Worker-delivered notifier status_update messages need to be forwarded to the agent's configured Hermes Slack home channels. Otherwise notifier delivery marks messages delivered but no Slack announcement is posted.

## Close Reason

Implemented worker forwarding of notifier status_update messages to configured Hermes Slack home channels with tests
