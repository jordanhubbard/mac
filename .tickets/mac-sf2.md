---
id: mac-sf2
status: closed
deps: []
links: []
created: 2026-05-18T05:51:31Z
type: feature
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-sf2
---
# Add built-in dashboard foundation

Create a dependency-free browser dashboard served by the mac FastAPI app, starting with overview and agents/workers views that explain control-plane state without copying the ACC dashboard wholesale.

## Design

Serve static assets from src/mac/ui using FastAPI StaticFiles and a /ui redirect. Keep the first slice read-only and implemented with plain browser modules so mac remains simple to modify.

## Acceptance Criteria

GET /ui serves a dashboard; static UI assets are served by FastAPI; the UI reads existing mac endpoints; the agents view links workers to machines, tasks, capacity, and eligibility signals.

## Close Reason

Implemented dependency-free FastAPI-served dashboard shell with overview, agents, tasks, Hermes, runtime, and rollout read-only views plus route tests.
