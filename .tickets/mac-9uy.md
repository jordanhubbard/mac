---
id: mac-9uy
status: closed
deps: []
links: []
created: 2026-05-19T02:46:31Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-9uy
---
# Add observability metrics logs and dashboard stream

Add a coherent observability layer to mac: low-level metric/log ingestion, automatic API/control-plane observations, query/subscription endpoints, and dashboard visualization.

## Acceptance Criteria

mac persists metric and log observations with layer/source/level/name metadata; API and control-plane paths record built-in observations; clients can list and stream observations; dashboard state and UI expose an observability view with live subscription; tests and docs cover the new surface.

## Close Reason

Done
