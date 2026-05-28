---
id: mac-02a
status: closed
deps: []
links: []
created: 2026-05-24T21:41:49Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-02a
---
# Expose service UI links and auth configuration in dashboard

Why: MAC operators need direct navigation to fleet service UIs after authenticating to the MAC dashboard, and need to see how each service is authenticated without leaking raw secrets. What: include TokenHub, Qdrant, and Firecrawl service links plus pass-through/auth capability metadata and redacted credential sources in /dashboard/state and render them in the dashboard.

## Close Reason

Implemented dashboard service links, redacted credential metadata, and TokenHub SSO handoff; uv run pytest -q passes.
