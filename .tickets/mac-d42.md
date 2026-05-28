---
id: mac-d42
status: closed
deps: []
links: []
created: 2026-05-23T22:00:01Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-d42
---
# Prepare cross-host review worktrees and git main publication

The live nanolang run proved the executor evidence path, but an off-host reviewer rejected valid Rocky evidence because it could not inspect Rocky-local paths. The full lifecycle proof needs reviewers to verify pushed repository branches from their own worktree and publication to actually fast-forward main for git://main targets.

## Acceptance Criteria

Review workers prepare a local review git worktree from executor evidence remote/ref/head metadata; review task metadata includes the review worktree, project, executor evidence and claimed work; git://main publication fast-forwards and pushes main when repository metadata is present; tests cover the review worktree and merge path.

## Close Reason

Implemented reviewer-local git worktree preparation from executor evidence, enriched repo evidence with remote metadata, added git://main fast-forward publication, updated reviewer prompt, and covered the path with regression tests. Full suite: 368 passed.
