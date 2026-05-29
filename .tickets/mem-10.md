---
id: mem-10
status: open
deps: [mem-07]
links: []
created: 2026-05-29T02:21:17Z
type: feature
priority: 3
assignee:
mac-task-id: task_a30cd07e8ede4d90a4e9fac5860f3db6
audit: memory-tier-2026-05-28
---
# Memory-tier health check + alerts (regression detector for silent consolidator)

**Symptom.** The audit found that the memory tier was supposed to exist but was inert — Qdrant collections empty, `vector_refs` empty, even though `mac.db` had grown to 3.1 GB. The gap was invisible until an external audit looked at it.

**Why it matters.** A silently-broken consolidation pipeline is the exact failure mode this work is supposed to prevent. We need a regression detector that catches the same class of bug in the future.

## Acceptance Criteria

- New CLI: `mac observability memory-health` returning:
  - `mac_db_size_bytes`
  - `observability_events_count`
  - `memory_records_count`
  - `vector_refs_count`
  - `qdrant_collection_points` per collection
  - `last_nap_run_at`
- Alerts (notifier_channels):
  - `vector_refs_count == 0` when `memory_records_count > 100` → critical
  - `last_nap_run_at` older than `2 × nap_interval` → critical
  - `mac_db_size_bytes` doubles within a week without proportional `vector_refs` growth → warning
- Optional: dashboard tile in the existing mac UI.

Blocked by mem-07 (need vector_refs to actually grow before the alert is meaningful).
