---
id: mac-35y
status: closed
deps: []
links: []
created: 2026-05-20T17:44:28Z
type: task
priority: 1
assignee: agent_natasha
mac-task-id: pending:mac-35y
---
# Deliver operator notification outbox to Hermes and Slack

The first implementation added a durable operator notification outbox and dashboard/API visibility, with lifecycle entries tagged for dashboard and hermes channels. Add an idempotent delivery runner that reads pending notifications, posts Hermes/Slack-visible messages through the configured Hermes home channel/gateway path, marks notifications delivered/failed/skipped, and records delivery telemetry without blocking task transitions.

## Notes

mac task task_13c955d3545745c8a8ec3cce3598069b failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
