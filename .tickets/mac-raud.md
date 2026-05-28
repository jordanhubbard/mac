---
id: mac-raud
status: closed
deps: []
links: []
created: 2026-05-27T00:54:43Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-raud
---
# Worker git clone <remote_url> permits argv smuggling from evidence metadata

src/mac/worker.py:1447-1448 (and :1471 for fetch) — git clone --no-checkout <remote_url> with remote_url pulled from evidence.metadata.verification.repo.remote_url (line 1428). No -- separator, no URL scheme/host validation. A peer worker writes evidence with remote_url='--upload-pack=/tmp/x' (or --config=...) and any reviewer-claim worker running that review executes attacker logic. Same pattern for remote_ref on fetch. Fix: add '--' separator, regex-validate URL scheme and shape, reject anything starting with '-'.

## Close Reason

Closed
