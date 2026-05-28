---
id: mac-e6k
status: closed
deps: []
links: []
created: 2026-05-20T15:34:15Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-e6k
---
# Add 24-hour command audit for mac agents

Record short-retention command start/completion events for mac-managed agent subprocesses so operators can verify agents are doing work without inferring from task state.

## Acceptance Criteria

Hub stores command audit rows with 24h retention; mac-agent reports subprocess start/completion/failure; deployed Hermes executor reports the Hermes command; dashboard/API/CLI expose recent rows; tests cover API and worker reporting.

## Close Reason

Implemented short-retention command audit storage, API, CLI, dashboard surface, mac-agent subprocess reporting, deployed Hermes executor reporting, docs, and tests. Verified with full pytest suite.
