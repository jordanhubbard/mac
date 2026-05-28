---
id: mac-29vr
status: closed
deps: []
links: []
created: 2026-05-27T00:55:15Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-29vr
---
# Observability has no retention policy or per-row byte cap

src/mac/observability_service.py — record_log fires on every agentbus open/append/read (agentbus_service.py:108, 190, 318) plus ~40 sites in services.py. No detail byte cap; prune is manual only (observability_service.py:175-206). A chatty reader can pump GB into one SQLite table. Fix: per-row detail size cap; default retention with periodic prune job.

## Close Reason

Closed
