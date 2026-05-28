---
id: mac-xc8u
status: closed
deps: []
links: []
created: 2026-05-27T00:53:37Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-xc8u
---
# No rate limit on /secrets/reveal or /secrets/access endpoints

src/mac/api.py:3323-3337 — a compromised secret-scoped token can enumerate every (secret_id, audit_id) pair at line rate; only the per-handle single-use guard limits damage. Fix: add per-principal rate limit and alarm on burst access patterns.

## Close Reason

Closed
