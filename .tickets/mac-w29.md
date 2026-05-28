---
id: mac-w29
status: closed
deps: []
links: []
created: 2026-05-20T04:50:06Z
type: bug
priority: 0
mac-task-id: pending:mac-w29
---
# Auto-review: default publication target 'mac://tasks/{id}' is a placeholder, not a destination

services.py:2486 _default_publication_target returns 'mac://tasks/{task_id}' when the operator hasn't set publication_target in task metadata. There is no resolver for that URI — it's filler that makes audit logs look meaningful when they aren't. In an autonomous swarm the publication is the persistent record of what got merged; the destination matters. Either honor task.metadata.publication_target (already supported) OR refuse to publish when no target is set, with observability event 'workflow.default_review.no_publication_target'. Stop silently inventing destinations.

## Close Reason

Closed
