---
id: mac-kc80
status: closed
deps: []
links: []
created: 2026-05-27T18:00:09Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-kc80
---
# Surface degraded Hermes provider state in agent health

After startup self-tests classify TokenHub budget_exceeded and let workers continue, the normal worker heartbeat currently overwrites the degraded startup health/resources so the hub API shows agents as healthy. Preserve or project the startup_self_test hermes_failure_class into agent health/resources until a successful Hermes smoke clears it, so the UI exposes token/provider exhaustion as a first-class degraded capability.

## Close Reason

Implemented in 361c8fa: agent re-registration and liveness heartbeats preserve resources.startup_self_test and project degraded health while startup self-test reports provider failure. Deployed to rocky, natasha, and bullwinkle; hub now reports all three idle/degraded with hermes_failure_class=budget_exceeded.
