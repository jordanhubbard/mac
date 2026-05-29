---
id: mac-ykkc
status: open
deps: []
links: []
created: 2026-05-28T03:29:19Z
type: bug
priority: 0
mac-task-id: pending:mac-ykkc
---
# Cap task.review_claimed retries; investigate 45k-row storm in task_history

rocky's task_history table has 52,755 rows; 45,017 of them are task.review_claimed for what should be fewer than 300 reviews ever. That is an order-of-magnitude retry storm. Side effect: 78 of 255 reviews were retracted, 52 of those for reviewer_unavailable:reviewer_unhealthy.

The retry mechanism for review claims (somewhere in services.py review_claim or worker.py review polling) appears to retry without backoff or attempt-cap when a reviewer is briefly unavailable, generating thousands of history rows and ultimately causing the dispatcher to declare the reviewer 'unhealthy' and retract the review.

Action:
1. Find the loop generating these rows (likely in worker.py review nudge handling or services.py review request retry).
2. Add an attempt cap (e.g., 20) and exponential backoff between claim attempts.
3. Distinguish 'reviewer is briefly busy' from 'reviewer is unhealthy' — don't escalate to retract until the cap is exceeded.
4. Add an event index on task_history.action so debug queries are not table scans.
5. Consider compacting the existing history rows (per-task review_claimed dedup with min/max timestamp + count).

## Acceptance Criteria

- New task.review_claimed rows per review are bounded (target: <= 20 over a review's lifetime)
- A reviewer briefly busy does not trigger retraction
- task_history query plan uses an index on action
- Existing storm rows are either compacted or quarantined; running totals after the fix show a steady-state ratio of review_claimed:reviews_total < 5

## Notes

2026-05-28 escalation: confirmed reproducible during e2e proof of mac-wsny. Test task task_e5043d35a817498db46e14b09286f21b transitioned to REVIEWING at 17:20:01; agent_natasha attempted to claim the review and storm-retried 167 times in 8 minutes (3-second intervals), each attempt hanging on `git fetch origin +refs/heads/mac/agent_bullwinkle/<lease>` because bullwinkle never pushed the lease branch to GitHub (evidence.repo.pushed=False). The reviewer cannot fetch what isn't there and has no circuit breaker; the task is permanently stuck in REVIEWING with the review row repeatedly re-claimed. This blocks every autonomous merge whose worker's git push fails for any reason.

Priority raised from P2 to P0 because it is the immediate blocker for autonomous-merge proof.

Action (urgent):
1. Cap review_claim retries on the reviewer side (e.g. 5 attempts within 1 minute), then mark the review FAILED with a clear reason so the parent task transitions to FAILED instead of spinning.
2. When the `git fetch` step for the source branch returns 0 refs / not_found, fail-closed immediately rather than re-claiming.
3. Add an observability counter for review_claim attempts per review id so this storm is visible in dashboards.
