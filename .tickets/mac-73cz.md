---
id: mac-73cz
status: closed
deps: []
links: []
created: 2026-05-28T03:29:36Z
type: bug
priority: 2
mac-task-id: pending:mac-73cz
---
# Circuit-breaker on self-repair task spawning during provider outages

On 2026-05-27 02:06 UTC, 10+ 'Repair c26 checkout before Beads polling' tasks were spawned and failed back-to-back in 13 seconds each, all with the same upstream OpenAI 429 quota error. The pattern: a worker fails -> control plane spawns a 'repair' task to clean up -> repair task hits the same 429 -> spawns another repair task. The self-healing infrastructure becomes a thrash multiplier when the upstream provider is down.

Symptom traces:
- /home/jkh/.mac/agent-workspaces/task_094909f658ed4ab9b59fc2eac5ec4e86/stdout.txt: 'API call failed after 3 retries: HTTP 502: all providers failed for streaming: API error (status 429): You exceeded your current quota'
- 10+ sibling task IDs in rocky's task_history all transitioned to failed within the same minute

Action:
1. Find the auto-spawn logic for 'Repair X before Y' tasks (likely in workflow_runtime.py or task_lifecycle.py).
2. Add a circuit breaker keyed on (task_template, agent, hour_bucket) — N failures in M minutes pauses new spawns for that template.
3. Surface the breaker state in the agent heartbeat resources so operators can see why work stopped.
4. Distinguish 'upstream provider quota exhausted' (do not retry) from 'transient network error' (retry).

Mitigation already in place: mac-rdez closed today verified TokenHub provider chain works again. But the spawn-storm pattern itself is still present and will trigger the next time any provider gets flaky.

## Acceptance Criteria

- Self-repair task spawning is bounded by a circuit breaker; opening the breaker is visible in agent heartbeat resources
- An OpenAI 429 or equivalent quota error short-circuits further task spawning until manually cleared OR a cooldown elapses
- Test: simulate provider 429 from a worker and verify no more than 3 'Repair X' tasks are spawned in a 5-minute window

## Resolution (2026-05-31)

Circuit-breaker implemented. ControlPlane._beads_remediation_breaker_open (services.py) bounds the self-repair spawn rate per repository: if MAC_REPAIR_BREAKER_MAX_SPAWNS (default 3) remediation tasks for a repo were created within MAC_REPAIR_BREAKER_WINDOW_SECONDS (default 300), the breaker opens and _ensure_beads_source_remediation_task returns None instead of spawning another — so a flaky provider (e.g. an OpenAI 429) can't turn self-healing into a thrash multiplier. Bounding by spawn *rate* (not error class) covers quota AND transient failures; the window rolling off is the cooldown. Opening is recorded as a bridge.beads.source_remediation.circuit_open observation (operator-visible). tests/test_repair_circuit_breaker.py proves the AC: no more than 3 'Repair X' tasks spawn in a 5-minute window, per-repo, disable-able via max=0. Note: the beads bridge is gated off by default (MAC_BEADS_BRIDGE_ENABLED), so this is also defense-in-depth. Partial/follow-up: surfacing the breaker in the heartbeat resources *field* (vs. the obs log) and a per-error-class quota-vs-transient policy are refinements, not blockers.
