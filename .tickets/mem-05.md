---
id: mem-05
status: open
deps: []
links: [mem-01]
created: 2026-05-29T02:21:07Z
type: feature
priority: 2
assignee:
mac-task-id: task_f99c67acf00b42a7880cc508ca333f0e
audit: memory-tier-2026-05-28
---
# Make task_history.review_claimed writes idempotent (schema-level dedupe)

**Symptom.** mem-01 documents a 30K-row write-amplification incident for a single review. Even after the finalizer bug is fixed, the schema permits this — `task_history` has no uniqueness constraint that would have rejected the 30,806th identical claim.

**Why it matters.** Schemas are the last line of defense. The review path is just one of many writers; the next regression in some other writer would silently fill the database the same way.

## Acceptance Criteria

- For `event_type='task.review_claimed'`: write becomes idempotent on `(task_id, review_id, worktree_digest)` — second attempt is a no-op (or returns the prior row id).
- For other write-amplification-prone history events (`task.lease_renewed`, `task.review_requested`, `task.review_retracted`), evaluate similar dedupe keys and apply where appropriate.
- Migration adds a partial unique index so existing duplicates can be collapsed offline by a separate cleanup task.
- Test: 100 sequential identical `record_review_claim` calls produce 1 row.

Depends on no other ticket; complements mem-01.
