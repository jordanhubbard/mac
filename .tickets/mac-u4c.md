---
id: mac-u4c
status: closed
deps: []
links: []
created: 2026-05-19T07:38:39Z
type: task
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-u4c
---
# Automate review and publication workflow for needs_review tasks

Live mac-agent workers now claim and execute tasks, but successful work stops at needs_review with evidence and no review/publication records. Add the default reviewer/verification path so completed executor output is assigned to an eligible reviewer, review decisions are recorded, and publication/completion gates advance when approved.

## Acceptance Criteria

A task that reaches needs_review is automatically assigned to a reviewer agent that did not own the task; review evidence and decisions are recorded; approved work can publish/complete through the default workflow; dashboard and observability show review/publish progression.

## Close Reason

fixed
