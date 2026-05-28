---
id: mac-4ks
status: closed
deps: []
links: []
created: 2026-05-19T07:04:22Z
type: task
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-4ks
---
# Enable live fleet task claiming

Switch rocky, natasha, and bullwinkle mac-agent services from heartbeat/canary-safe mode into live task-claiming loop mode, redeploy, and monitor claims/execution/review workflow telemetry.

## Acceptance Criteria

Fleet configs commit live worker loop mode; deployment succeeds on all three agents; hub telemetry/logs show tasks being claimed, executed, submitted for review or workflow-progressed without systemic failures.

## Close Reason

Enabled loop-mode live claiming for rocky, natasha, and bullwinkle; hardened deploy against artifact replacement races; requeued 143 infra-failed tasks; observed all three agents claiming/running tasks and five successful executor completions reaching needs_review with evidence.
