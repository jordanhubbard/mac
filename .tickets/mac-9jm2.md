---
id: mac-9jm2
status: closed
deps: []
links: []
created: 2026-05-27T00:55:02Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-9jm2
---
# _append_beads_ledger_comment is not idempotent — duplicate ledger rows on retry

src/mac/services.py:9053-9090 — appends blindly; if the server retries (e.g., bd command times out at 20s but actually wrote), you get duplicate mac-ledger v1 task=X event=... rows on the bead. No fingerprint, no dedupe key. Fix: include an idempotency token in the comment body and check existing comments before re-posting, or move to a comment-update model.

## Close Reason

Closed
