---
id: mem-12
status: open
deps: []
links: [mem-01, mem-11, mem-13]
created: 2026-05-29T02:58:02Z
type: bug
priority: 1
assignee:
mac-task-id: task_cba0c4b6add74f2e9bf1c9e495170b7e
audit: memory-tier-2026-05-28
discovered_via: mem-01
---
# Bound review retraction: transition task to failed after N retracts

**Discovered in mem-01 root cause.** Task `task_d7c51a0b...` has **503 review records**. Each review reaches 10 retry attempts, retracts with reason `reviewer_unable_to_produce_verdict_after_10_attempts`, then the workflow **creates a new review for the same `executor_evidence_id`** and the cycle repeats. There is no upper bound on the number of reviews that can be opened against a single piece of evidence.

This is independent of why the underlying evidence is bad (mem-11 covers that). Even if a previously-good evidence somehow becomes unreviewable later (remote branch deleted, repo migrated, etc.), the system should give up gracefully rather than thrash forever.

## Acceptance Criteria

- After N (default 3, configurable per fleet) consecutive retracted reviews against the same `(task_id, executor_evidence_id)`, the workflow does NOT create another review. Instead:
  - Transition the task to `state='failed'` (or a new `state='blocked'` if we want operator triage), reason captures the last retraction reason.
  - Emit a `task.review_exhausted` event and an operator notification.
- Counter resets when a new piece of evidence is recorded (operator submits a different evidence, retry path).
- Test: simulate 4 retractions; assert the 4th does not create a new review and the task moves to failed.
- Backfill: on rollout, scan existing tasks with > N retracted reviews and transition them.

Discovered during [mem-01](mem-01.md) root cause analysis on 2026-05-29.
