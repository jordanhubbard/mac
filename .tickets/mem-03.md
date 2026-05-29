---
id: mem-03
status: open
deps: []
links: []
created: 2026-05-29T02:21:03Z
type: bug
priority: 1
assignee:
mac-task-id: task_53d651f749154b41bb49b174b939d3a5
audit: memory-tier-2026-05-28
---
# Silence beads bridge log spam (66K+ events with bridge disabled)

**Symptom.** `CLAUDE.md` says: *"The legacy beads (`bd`) integration is shut off"*, `MAC_BEADS_BRIDGE_ENABLED` is gated off by default. Yet on rocky:

- `bridge.beads.sync_busy` — **36,012** rows
- `bridge.beads.repository_source` — **31,833** rows

Plus `task.beads_retry_exhausted` (38 rows) and `beads_repositories` table still being touched. Either the bridge is firing despite the gate, or the disabled path still emits "I'm skipping" log rows on every poll.

## Acceptance Criteria

- When `MAC_BEADS_BRIDGE_ENABLED` is unset/false, the bridge produces zero `observability_events` rows under any name (not even "skipped").
- Verified by running the worker loop for 10 minutes with the gate off and asserting zero new `bridge.beads.*` rows.
- If there's a legitimate "bridge offline" health metric, demote it to one `metric` row per hour (not per-poll `log`).
