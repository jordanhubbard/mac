---
id: mac-s0ta
status: closed
deps: []
links: []
created: 2026-05-27T00:54:06Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-s0ta
---
# expire_leases is not atomic across read and write — silent lease theft

src/mac/services.py:4651-4718 — expire_leases reads expired leases via query_all and per-lease get_task outside the transaction, then opens a fresh transaction per lease without re-asserting leases.status=ACTIVE or tasks.lease_id=lease.id in the UPDATE WHERE clauses. Between read and write, renew_lease/release_lease/another expire pass can flip the lease; this run will then null out a task that has been validly re-claimed, stealing it from the new owner. The companion path _expire_agent_active_leases at services.py:11367 correctly guards with WHERE id=? AND lease_id=?. Fix: mirror the guarded UPDATE pattern in expire_leases.

## Close Reason

Closed
