---
id: mac-8vn
status: closed
deps: []
links: []
created: 2026-05-20T20:15:07Z
type: task
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-8vn
---
# Resolve repository worktree sources per host

The first worktree rollout moved checkout preparation into mac-agent, but task metadata can contain the hub's absolute repository_path. Workers on other OSes must resolve that declared path to their host-local registered checkout before creating task-owned worktrees.

## Close Reason

Added host-local repository source resolution for worktree creation, including self-update repo fallback for mac/repo-beads-mac tasks and regression coverage for hub-path metadata on non-hub workers. Verified with uv run pytest: 288 passed.
