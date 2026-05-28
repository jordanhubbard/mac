---
id: mac-33z
status: closed
deps: []
links: []
created: 2026-05-20T20:22:16Z
type: task
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-33z
---
# Redact worker tokens from deploy status output

Fleet deploy prints systemctl status for mac-agent.service, which includes the full mac-agent command line and bearer token. Replace status collection with a redacted service summary or run mac-agent with token via env/file-only argv so deployment logs cannot capture secrets.

## Close Reason

Replaced mac-agent systemctl status output with systemctl show summary fields so deploy logs no longer print worker command lines or bearer tokens; added regression coverage.
