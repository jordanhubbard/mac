---
id: mac-853j
status: closed
deps: []
links: []
created: 2026-05-27T00:52:48Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-853j
---
# Auth fail-open + 0.0.0.0 hub bind exposes /secrets/reveal on network

When MAC_API_TOKEN/MAC_API_TOKENS is unset, _authorize_request returns admin for every route (src/mac/api.py:942, 130-132). Fleet config defaults control_bind_host to 0.0.0.0 on the hub (deploy/fleet/config.yaml:85, scripts/setup-fleet.py:389,546). A hub deployed without the env var exposes /secrets/{id}/reveal and /agentbus/repo-update to anyone reachable on the bind interface, with no audit linkage to a real caller. Fix: refuse to start when bound to a non-loopback interface without auth tokens configured, or fail-closed and print a startup error.

## Close Reason

Closed
