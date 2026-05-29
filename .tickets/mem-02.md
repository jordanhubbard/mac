---
id: mem-02
status: open
deps: []
links: []
created: 2026-05-29T02:21:01Z
type: bug
priority: 1
assignee:
mac-task-id: task_addf7e07a9d04e819c5cab323dd6bdc7
audit: memory-tier-2026-05-28
---
# Schedule observability_events prune (no caller wired)

**Symptom.** `ObservabilityService.prune(older_than=..., keep_last=...)` exists in `src/mac/observability_service.py:181` and is exposed via `ControlPlane.prune_observability`, but **nothing calls it**. No systemd timer, no in-process scheduler, no cron entry on rocky (`crontab -l` confirms — only an unrelated `prune-beads-rebuild.sh`).

Result on rocky as of 2026-05-29:
- `observability_events` = **2,088,341 rows** (10 days of data)
- Current write rate ≈ **30K rows/hour** (last hour) / **223K rows/day**
- `mac.db` is **3.1 GB**

## Acceptance Criteria

- A scheduled job (systemd timer in `deploy/systemd/` or an internal periodic) calls `prune_observability` daily.
- Default retention: keep `log` rows for 7 days, `metric`/`event` rows for 30 days, configurable per fleet.
- After first run on rocky, `observability_events` row count drops to fit in the retention window.
- Pruning is logged as a `metric` so its effect is observable.

Depends on no other ticket — this is a standalone hygiene fix.
