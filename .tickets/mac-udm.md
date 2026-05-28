---
id: mac-udm
status: closed
deps: []
links: []
created: 2026-05-24T10:43:20Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-udm
---
# Expose Hermes first-class task list

MAC already exposes /tasks and mac task list, but Hermes only has work-context and task-detail for task discovery. Add a direct list_tasks operation to the Hermes adapter and mac-hermes CLI, include list_tasks and mac task list/mac-hermes tasks in the first-class task proof contracts and runtime context, and update tests/docs so tasks have list/detail parity like projects and agents.

## Close Reason

Added direct Hermes task list support through adapter.list_tasks and mac-hermes tasks, updated first-class task API/CLI/runtime proof contracts, docs, and tests.
