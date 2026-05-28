---
id: mac-eya
status: closed
deps: []
links: []
created: 2026-05-21T19:56:20Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-eya
---
# Fix stale review routing and Beads DB sync blockers

Default review routing can strand reviews on stale synthetic reviewer agents, and Beads bridge polling can use a dedicated checkout with an embedded Beads DB that was not pulled from the Dolt remote. Fix reviewer liveness/reassignment and sync Beads DB state during bridge bootstrap/poll so live fleet agents can claim/review canonical work.

## Close Reason

Fixed stale reviewer reassignment and Beads Dolt pull during bridge/deploy bootstrap; verified with pytest.
