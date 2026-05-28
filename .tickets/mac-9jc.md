---
id: mac-9jc
status: closed
deps: []
links: []
created: 2026-05-18T05:57:51Z
type: task
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-9jc
---
# Add dashboard read-model endpoints

Create read-only aggregate endpoints for the dashboard so the browser does not reconstruct complex state from many resource collections as the fleet grows.

## Design

Derived API surfaces should be read-only and built from existing control-plane tables/services.

## Acceptance Criteria

Expose overview, agent detail, task timeline, dispatch explanation, Hermes activity, and rollout status read models without creating parallel durable truth.

## Close Reason

Implemented /dashboard/state plus agent detail, task timeline, dispatch explanation, Hermes activity, and rollout status read-model routes used by the dashboard.
