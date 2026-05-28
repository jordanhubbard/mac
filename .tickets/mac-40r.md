---
id: mac-40r
status: closed
deps: []
links: []
created: 2026-05-25T18:39:50Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-40r
---
# Enforce required files and main publication for MAC tasks

MAC should prevent epic work from being stranded on task branches or accepted without the expected human-facing files. Add control-plane checks so repository tasks can declare required changed files/acceptance surface, review evidence must include those files, and git publication verifies the reviewed branch is contained in main before completion.

## Close Reason

Implemented required changed-file enforcement in control-plane review/publication paths and worker pre-submit validation; tightened git://main publication to require a real repository path; full test suite passes.
