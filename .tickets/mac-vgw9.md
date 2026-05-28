---
id: mac-vgw9
status: closed
deps: []
links: []
created: 2026-05-27T00:54:29Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-vgw9
---
# Lease expiry uses wall-clock ISO strings — NTP step vulnerability

src/mac/services.py:4253, src/mac/models.py:34-35 — expires_at is text compared lexicographically. Any code path writing a different timespec produces strings that sort wrong. No monotonic clock; NTP step backwards delays expiry, forwards mass-expires active leases. No documented tolerance. Fix: normalize timestamps; add jitter tolerance; consider integer epoch microseconds.

## Close Reason

Closed
