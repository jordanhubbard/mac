---
id: mac-9kij
status: closed
deps: []
links: []
created: 2026-05-27T00:53:19Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-9kij
---
# Reviewer worktree_digest only string-matches executor claim — no real git re-verification

src/mac/services.py:10906-10923 — the reviewer's verdict must declare worktree_digest and repo.head_sha matching the executor's, but mac never re-runs git ls-remote, checks the PR is open, or confirms the SHA is on origin/<remote_ref>. Trust reduces to 'the reviewer typed the same SHA the executor typed' plus an HMAC. _publish_git_target_if_needed (services.py:6296-6420) does a real fast-forward only for git://main targets; other targets are trust-only. Fix: server-side ls-remote / SHA-reachability check before publication.

## Close Reason

Closed
