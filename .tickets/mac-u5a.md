---
id: mac-u5a
status: closed
deps: []
links: []
created: 2026-05-20T01:13:07Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-u5a
---
# Refresh agent liveness during lease renewal

Busy workers renew task leases without refreshing agents.last_seen_at, so long-running tasks can make an alive worker look stale at the agent row. Update the lease/heartbeat contract so active lease renewal also records agent liveness without allowing invalid idle heartbeats while busy.

## Close Reason

fixed
