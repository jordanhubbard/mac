---
id: mac-evd
status: closed
deps: []
links: []
created: 2026-05-18T05:53:26Z
type: task
priority: 2
mac-task-id: pending:mac-evd
---
# Agent build versioning via running_digest on heartbeats

## Close Reason

agents.running_digest column + migrations; heartbeat validates against runtime_environments.digest; fleet_build_distribution aggregates live agents by build. API /fleet/build-distribution and CLI 'mac fleet build-distribution'. Tests cover heartbeat validation and aggregation.
