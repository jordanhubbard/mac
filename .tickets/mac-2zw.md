---
id: mac-2zw
status: closed
deps: []
links: []
created: 2026-05-20T19:53:58Z
type: task
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-2zw
---
# Run repository tasks in isolated git worktrees

Agents must never edit registered or production main checkouts directly. Repository tasks should be prepared in task-owned git worktrees, executors should receive that worktree as the only writable repo path, and dirty registered checkouts should fail closed with telemetry/remediation instead of being used as task workspaces.

## Close Reason

Implemented task-owned repository worktree preparation for normal repository tasks, exported worktree env/runtime metadata to subprocess executors, failed dirty registered checkouts closed with telemetry, preserved source-remediation tasks as explicit exceptions, tightened Beads dirty checks to include untracked files, and added regression coverage. Verified with uv run pytest: 287 passed.
