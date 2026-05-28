---
id: mac-3qv6
status: closed
deps: []
links: []
created: 2026-05-27T00:54:59Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-3qv6
---
# Worktree cleanup leaks .git/worktrees registration on crash

src/mac/worker.py:1306-1312 — _prepare_repository_worktree deletes worktree_dir with shutil.rmtree on next prepare, but never calls git worktree remove / prune. Crashed worker leaves orphaned .git/worktrees/ entries inside source_root; the next attempt sees 'already exists' and bails. Fix: call git worktree prune at startup; use git worktree remove for cleanup.

## Close Reason

Closed
