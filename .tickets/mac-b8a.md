---
id: mac-b8a
status: closed
deps: []
links: []
created: 2026-05-23T22:26:27Z
type: task
priority: 0
assignee: agent_bullwinkle
mac-task-id: pending:mac-b8a
---
# Lifecycle proof: merge reviewed Beads task to main

This is a live end-to-end lifecycle proof for the Rocky/Natasha/Bullwinkle fleet.

In the repo-beads-mac project, using MAC's task-owned GitHub worktree, create or update docs/agent-lifecycle-proof.md. Add one short bullet containing:

- the Bead ID for this task
- the UTC date 2026-05-23
- the phrase "git-main review lifecycle proof"
- the MAC task title

Do not change runtime behavior and do not touch unrelated files.

Run the repository contract test command before submitting evidence:

PATH=.venv/bin:$PATH .venv/bin/python -m pytest

Commit the documentation change on the task-owned branch, push that branch to GitHub, and provide repo_change evidence that includes the pushed branch/ref, remote URL, head SHA, dirty=false, pushed=true, files_changed containing only docs/agent-lifecycle-proof.md, and the test result summary.

## Acceptance Criteria

MAC imports this Bead as a repo-beads-mac task with publication_target git://main; a fleet agent claims it and performs the documentation change in a task-owned GitHub worktree; a different fleet agent claims the review with metadata showing worktree, project, head/ref, and work summary; the reviewer builds/runs the repository test contract and records an approved review verdict; MAC publishes by fast-forwarding main; the Bead is closed with ledger comments; Slack home-channel task transition notifications are emitted.

## Close Reason

Superseded by a fresh lifecycle proof after fixing the fleet contract test environment. This attempt reached review, but Rocky correctly rejected it because the inherited live-agent env made the repository contract test fail in review.
