---
id: mac-n5x
status: closed
deps: []
links: []
created: 2026-05-19T01:14:16Z
type: task
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-n5x
---
# Support Hermes home channel in mac agent config

Allow mac fleet agent configuration files to declare the Hermes Slack home channel name, matching ACC's ACC_SLACK_HOME_CHANNEL_NAME behavior, and propagate that into Hermes Slack account/home-channel sync during deployment/startup.

## Acceptance Criteria

Per-agent mac config can set a Hermes home channel name; deployment/startup uses that value when writing slack_home_channels.json or related Hermes env; existing env overrides continue to work; tests cover configured value and fallback behavior.

## Close Reason

Done
