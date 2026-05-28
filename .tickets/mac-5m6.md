---
id: mac-5m6
status: closed
deps: []
links: []
created: 2026-05-24T21:00:24Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-5m6
---
# Complete TokenHub fleet bootstrap and startup readiness

Fix remaining startup warnings that block clean Hermes readiness and verify every fleet hub and agent has reachable TokenHub service/client credentials after deployment. Agents are reporting API errors, so deployment must confirm hub TokenHub health, provider vault storage, aliases, and worker TokenHub env propagation.

## Close Reason

TokenHub bootstrap verified on Rocky; fixed Hermes gateway shim to force deployment-managed TokenHub credentials over stale custom credential-pool entries; redeployed Rocky, Natasha, and Bullwinkle cleanly with direct TokenHub chat checks and no post-deploy 401/invalid-key logs.
