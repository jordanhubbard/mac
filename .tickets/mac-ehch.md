---
id: mac-ehch
status: closed
deps: []
links: []
created: 2026-05-27T00:54:50Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-ehch
---
# SubprocessExecutor timeout defaults to None → hung executor infinite lease renewal

src/mac/worker.py:2722 (CLI --timeout default None) and worker.py:566-625 (renew thread). The renew thread only stops when subprocess.run returns; with no timeout, a wedged executor renews its lease forever. Server-side expire_leases cannot recover the task because lease.expires_at keeps getting bumped. Fix: enforce a default timeout (e.g., task-declared budget, fallback 1h); make renew thread monitor heartbeat success rate and abort on persistent failures.

## Close Reason

Closed
