---
id: mac-xqn6.6
status: deferred
deps: []
links: []
created: 2026-05-26T17:39:46Z
type: bug
priority: 1
parent: mac-xqn6
mac-task-id: pending:mac-xqn6.6
---
# Fix review workflow handling for unpublished worker branches

Review workers are repeatedly failing to fetch refs like refs/heads/mac/... because executor evidence can reference branches that were never pushed or are on a different remote. Review should fail fast with a clear state transition instead of endlessly nudging reviewers.

## Acceptance Criteria

Executor evidence requiring review must include a fetchable remote ref or PR URL; missing remote refs cause task failure/blocking with one actionable finding, not repeated review nudge errors; review workers validate remote reachability before claiming; tests cover missing branch, wrong remote, and read credential failure.
