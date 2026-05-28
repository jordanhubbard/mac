---
id: mac-vtl
status: closed
deps: []
links: []
created: 2026-05-19T07:38:50Z
type: task
priority: 1
assignee: agent_natasha
mac-task-id: pending:mac-vtl
---
# Add active-worker drain to fleet deploy

The hardened deploy now stops services before replacing artifacts, preventing workers from claiming during the venv/Hermes replacement window. It still cannot safely deploy a new worker binary while agents are actively executing tasks, because stopping mac-agent interrupts the current Hermes subprocess. Add active lease detection and a drain/wait mode so code rollouts can wait for current tasks to finish, pause new claims, then restart workers without losing work.

## Acceptance Criteria

deploy/deploy-mac-fleet.sh detects active leases before stopping mac-agent; operators can choose wait/drain behavior; workers can pause new claims while completing current work; deploy verifies no running task is interrupted; rollout logs show drain decisions.

## Notes

mac task task_bbc7fb9a72db422c9ec820999791604b failed: beads_failed_task_retry_limit
mac task task_bbc7fb9a72db422c9ec820999791604b failed: beads_failed_task_retry_limit

## Close Reason

Implemented and verified with focused tests: review verdict git anchoring/independent check coverage and fleet deploy active-worker drain behavior.
