---
id: mac-cde
status: closed
deps: []
links: []
created: 2026-05-22T16:45:31Z
type: feature
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-cde
---
# Report agent task progress to Slack home channels

When an agent with a Hermes/Slack connection changes task state, report that progress to the configured Slack home channel for each connected Slack workspace/server so operators can see agent task progress outside the mac dashboard and Beads ledger.

## Notes

Slack-specific worker-side progress reporter spike was discarded after scope changed to a generalized notifier integration. Future work should implement notifier configuration and Hermes-backed delivery generically across supported channels rather than hard-coding Slack in mac-agent.

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
