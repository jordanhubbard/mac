---
id: mac-glh0
status: closed
deps: []
links: []
created: 2026-05-27T00:53:32Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-glh0
---
# MAC_API_TOKEN plaintext in env; no hash-at-rest, no rotation, no revocation

src/mac/api.py:879-888, 110-124 — tokens are compared in-process with hmac.compare_digest (correct) but stored only as plaintext in env / mac.env. Leaked env file = full admin. No rotate API, no per-token issued_at, no revocation list. Fix: store hashed tokens, add rotation + revocation endpoints, support per-token expiry.

## Close Reason

Closed
