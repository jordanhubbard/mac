---
id: mac-z1g
status: closed
deps: []
links: []
created: 2026-05-26T05:25:11Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-z1g
---
# Add task timing, child tasks, and Hermes MAC vocabulary

Implement explicit task timing fields, a child-task API for parent/blocked-by task decomposition, and concise Hermes runtime vocabulary covering MAC first-class objects and APIs.

## Acceptance Criteria

Tasks expose started_at, completed_at, and last_updated_at; parent tasks can create child tasks and become blocked on them; API/CLI tests cover the workflow; Hermes runtime skill/context mentions first-class MAC objects and API vocabulary concisely.

## Close Reason

Implemented task timing fields, child-task blocking API/CLI, and concise Hermes MAC vocabulary.
