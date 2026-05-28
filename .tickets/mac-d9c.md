---
id: mac-d9c
status: closed
deps: []
links: []
created: 2026-05-20T04:49:50Z
type: bug
priority: 0
mac-task-id: pending:mac-d9c
---
# Auto-review: _default_review_for_task picks arbitrarily when multiple pending reviews exist

services.py:2453 _default_review_for_task falls through pending -> approved -> any. For a task with multiple pending reviews this silently picks the latest and proceeds. In an autonomous system ambiguous review state is more dangerous than in a human system because no operator breaks the tie. Refuse to act when there is more than one open review for the task; emit an observability event 'workflow.default_review.ambiguous' and leave the task in NEEDS_REVIEW/REVIEWING for explicit resolution.

## Close Reason

Closed
