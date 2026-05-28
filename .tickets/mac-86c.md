---
id: mac-86c
status: closed
deps: []
links: []
created: 2026-05-19T06:27:23Z
type: task
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-86c
---
# Add real hub worker process E2E

Add an end-to-end test that starts a real uvicorn mac hub against a temporary SQLite database and runs mac-agent as a subprocess to validate heartbeat/dry-run/loop execution over HTTP rather than in-process TestClient.

## Close Reason

Added real uvicorn hub + mac-agent subprocess E2E and fixed needs_review lease release discovered by the process-boundary test.
