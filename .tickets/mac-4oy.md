---
id: mac-4oy
status: closed
deps: []
links: []
created: 2026-05-19T03:42:34Z
type: task
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-4oy
---
# Deploy mac-agent hub-spoke worker service

MAC fleet deploy currently starts per-host control planes and Hermes gateways but does not start a mac-agent worker registration loop. Rocky must be the shared hub at http://100.125.137.89:8789, while Natasha and Bullwinkle register there as spokes. Add deploy support for hub URL/token propagation, a persistent worker service on systemd and launchd, health verification, and documentation.

## Close Reason

Implemented hub-spoke deploy support: Rocky binds as the hub, fleet deploy installs mac-agent services, spokes register against Rocky, registration is verified during deploy, docs/config updated, and the latest deployment shows rocky/natasha/bullwinkle healthy in the hub registry.
