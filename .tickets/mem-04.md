---
id: mem-04
status: closed
deps: []
links: []
created: 2026-05-29T02:21:05Z
type: bug
priority: 1
assignee:
mac-task-id: task_044b133397314b169dfff76a864f3845
audit: memory-tier-2026-05-28
---
# Demote polling chatter from log to metric kind (1.83M of 2.09M rows)

**Symptom.** The top of the observability volume is per-poll log spam, not actionable signal:

| Name | Rows |
|---|---|
| `worker.routing.no_candidate` | 627,470 |
| `worker.no_task` | 627,449 |
| `workflow.default_review.waiting_for_verdict` | 283,136 |
| `workflow.default_review.waiting` | 110,553 |
| `workflow.default_review.heartbeat_tick` | 94,362 |
| `agent.heartbeat_updated` | 92,756 |

Combined: **1.83M of the 2.09M total observability rows.** These are "still polling, nothing happened" events — they should be `metric` rows incremented per poll, not per-poll `log` rows with full `detail` JSON.

## Acceptance Criteria

- The six emitters above are converted from `kind='log'` to `kind='metric'`, written as counter increments (rolled up per minute or hour, not per poll).
- After conversion, write rate to `observability_events` drops by >80% on an idle hub.
- Per-poll detail (which worker, which task, etc.) is preserved on `level='warning'` or higher branches only — silent-success polls leave no row.
- Tests assert that `observability_events` row count for `worker.no_task` does not grow during an idle minute.

## Resolution (2026-05-31)

Implemented + tested (polling-chatter log names suppressed at record_log; MAC_OBSERVABILITY_VERBOSE_POLL to re-enable).
