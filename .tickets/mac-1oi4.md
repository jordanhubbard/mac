---
id: mac-1oi4
status: closed
deps: []
links: []
created: 2026-05-27T00:55:11Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-1oi4
---
# provisioning fulfill_request lacks two-party check

src/mac/provisioning_service.py:219 — fulfill_request(request_id, agent_id) accepts any agent id without verifying (a) agent role/capabilities/hardware match the request, (b) the fulfiller is a different actor from the requester. A compromised dispatcher can request-then-fulfill itself with an attacker-controlled agent. Fix: enforce capability/role match in fulfill; require different actor than requester (or explicit auto-approve flag).

## Close Reason

Closed
