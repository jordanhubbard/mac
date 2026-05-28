---
id: mac-uw3
status: closed
deps: []
links: []
created: 2026-05-18T19:40:18Z
type: task
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-uw3
---
# Deploy mac fleet replacement smoke

Deploy mac as the ACC replacement on rocky, natasha, and bullwinkle, redeploy upstream Hermes with startup shim handling, and inspect migration/startup logs for ACC state import and Hermes state readiness.

## Notes

Deployed mac replacement to rocky, natasha, and bullwinkle via deploy/deploy-mac-fleet.sh. Upstream NousResearch/hermes-agent was recloned on each host and the multi-Slack MVP patch applied. mac service and mac-managed Hermes gateway are active on all three hosts. Rocky one-time ACC migration imported 143/143 planned tasks with 146 provenance rows, 0 errors, and preserved private conversation/session tables as skipped state. Natasha and Bullwinkle had no ACC SQLite DB under ~/.acc/data; Hermes startup reports are ready with existing state refs and Slack shim present. Follow-up mac-6sy tracks inherited Hermes warning policy for HERMES_REDACT_SECRETS=false and benign Slack file_public warnings.

## Close Reason

mac replacement deploy completed and verified on rocky, natasha, and bullwinkle
