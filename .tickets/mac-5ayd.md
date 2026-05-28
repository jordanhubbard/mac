---
id: mac-5ayd
status: closed
deps: []
links: []
created: 2026-05-27T00:54:15Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-5ayd
---
# Dispatch loads ALL OPEN tasks into Python every tick (O(N) per scan)

src/mac/services.py:10279, 10277-10293 — list_tasks(OPEN) has no LIMIT; _dispatch_ordered_tasks then Python-sorts and round-robins in memory. tick(limit=100) calls dispatch_once 100 times, each re-running the full scan. claim_next_for_agent also calls _dispatch_ordered_tasks. At 100k+ open tasks this is unworkable. Fix: add a SQL LIMIT and DB-side ordering with appropriate index.

## Close Reason

Closed
