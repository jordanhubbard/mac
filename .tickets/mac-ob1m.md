---
id: mac-ob1m
status: closed
deps: []
links: []
created: 2026-05-27T00:55:20Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-ob1m
---
# Streaming endpoints poll without checking client disconnect

src/mac/api.py:3109-3133 (observability_stream), :3221-3258 (agentbus_stream_events) — generators sleep on poll_interval_seconds and never inspect request.is_disconnected(); each iteration issues a full DB query. Slow/abandoned clients keep working for timeout_seconds (≤30s) per connection; no per-token concurrency cap. Fix: check is_disconnected() per iteration; add per-principal max concurrent streams.

## Close Reason

Closed
