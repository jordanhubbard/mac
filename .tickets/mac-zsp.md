---
id: mac-zsp
status: closed
deps: []
links: []
created: 2026-05-23T21:42:14Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-zsp
---
# Worker rejects no-op Beads source remediation evidence

Rocky produced valid Beads source-remediation repo_change evidence with an empty files_changed list after confirming nanolang was already clean, but the worker pre-submit gate still rejected it even though the hub validator allows this remediation case.

## Acceptance Criteria

Worker submission validation allows empty repo.files_changed for repo_change evidence when task metadata marks the task as beads_source_remediation or beads_source_refresh; regression test covers the worker path.

## Close Reason

Fixed worker pre-submit validation to honor the Beads source-remediation no-op repo_change exception and added a regression test for empty files_changed evidence.
