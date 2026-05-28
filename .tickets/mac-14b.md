---
id: mac-14b
status: closed
deps: []
links: []
created: 2026-05-21T22:29:52Z
type: feature
priority: 2
mac-task-id: pending:mac-14b
---
# Add new-cluster / first-hub setup wizard

Today a new operator wanting to bring up their first mac hub has no guided path. The only deploy tool is deploy/deploy-mac-fleet.sh, which assumes: a hardcoded DEFAULT_HOSTS list (rocky/natasha/bullwinkle), a hardcoded hub at rocky's Tailscale IP (MAC_DEPLOY_HUB_AGENT=rocky, MAC_DEPLOY_HUB_URL=http://100.125.137.89:8789), SSH on port 22 only (uses bare 'ssh "$target"' — custom ports require ~/.ssh/config aliasing), and a pre-existing deploy/agents/<name>/config.env. There is no validation, no SSH preflight, no rollback story for first-time use, and no docs/getting-started.md. docs/production-deployment.md exists but is written for fleet replacement, not bootstrapping. Concrete asks: (1) a 'mac deploy init <name>' or 'deploy-mac-fleet.sh --new-hub <name> --target user@host[:port]' flow that scaffolds the agent config, runs SSH preflight, and deploys with the new host as hub; (2) explicit --ssh-port support in the deploy script so custom ports don't require editing ~/.ssh/config; (3) a getting-started doc covering 'I have one fresh box, make it a mac hub.' Discovered while trying to deploy a new hub to horde@20.115.163.162:2201.

## Acceptance Criteria

Operator with one fresh Linux box can run a single documented command and end up with a working mac hub, including SSH preflight, agent config scaffolding, and custom-port support.

## Notes

mac task task_44094d6b9f8b4743a743984865b2d2bb failed: beads_failed_task_retry_limit
mac task task_44094d6b9f8b4743a743984865b2d2bb failed: beads_failed_task_retry_limit
mac task task_44094d6b9f8b4743a743984865b2d2bb failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
