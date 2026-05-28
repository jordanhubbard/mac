---
id: mac-e7wx
status: closed
deps: []
links: []
created: 2026-05-27T21:14:24Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-e7wx
---
# Publish passed startup self-test reports to hub

The mac-agent startup self-test writes a passed report locally but only posts startup_self_test resources to /agents/{id}/heartbeat on failed/degraded runs. After TokenHub recovered, rocky/natasha/bullwinkle had local passed self-tests but the hub still showed stale degraded startup_self_test resources. The self-test wrapper should always publish the report and set health_status=healthy on pass.

## Close Reason

Startup self-test now publishes passed reports with health_status=healthy, and live rocky/natasha/bullwinkle hub records were refreshed to healthy.
