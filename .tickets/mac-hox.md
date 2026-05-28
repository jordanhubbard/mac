---
id: mac-hox
status: closed
deps: []
links: []
created: 2026-05-20T05:11:00Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-hox
---
# Add AgentBus self-update restart message

Add a signed AgentBus message type that tells listening agents to pull their local mac repository and restart themselves only when the local repo advances, so fleet development updates can be broadcast instead of manually redeploying every host.

## Close Reason

Implemented repo-update AgentBus control topic, mac-agent listener handling with git pull --ff-only and restart-on-change, result streams, API/CLI broadcast helpers, deploy self-update worktree setup, docs, and focused tests.
