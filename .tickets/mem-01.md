---
id: mem-01
status: open
deps: []
links: [mem-05]
created: 2026-05-29T02:20:59Z
type: bug
priority: 1
assignee:
mac-task-id: task_51286c7e3de043be813176a610590270
audit: memory-tier-2026-05-28
---
# Stop runaway task.review_claimed loop (executor finalizer regression)

**Symptom (rocky mac.db, 2026-05-28).** `task_history` has `task.review_claimed` rows piling up at ~2s intervals with identical `review_id`, `executor_evidence_id`, `worktree_digest`, and `work_summary` payload. Worst case:

- `task_1c4c606d763345df81e5383d75b1b5de` — **30,806** review-claim rows for a single review
- `task_7a9aa9304f284a9b813119fc345cf186` — 13,208
- `task_d7c51a0b04bd464787c9b43014702693` — **6,524 and still rising** at audit time (state still `reviewing`, **503 reviews** created, every one retracts after 10 attempts)

Sample `work_summary`: literal `"hello hello hello..."` × 32.

## Root cause (verified 2026-05-29)

This is not a finalizer bug. The finalizer never gets to run because the executor's evidence is invalid in a way the validator never noticed:

Executor evidence `ev_1c37a40b672e4e65891c988537c0025f` (created by `agent_bullwinkle` at 2026-05-28T17:45):
- `evidence_type: operator_result` ← **wrong type for a code task**
- `head_sha == base_sha == 5a0b31455b...` — **no commit happened**
- `files_changed: []`
- `pushed: false` — executor itself said the branch was never pushed
- `summary: "hello hello hello..."` × 32 — the LLM literally echoed greetings
- `checks: [{name:"hermes_chat_query", returncode:0, status:"pass"}]` — only check was "can Hermes chat"

The evidence passed validation because `OperatorResultValidator.validate()` (`evidence_validators.py:222`) only requires *any* summary string — no repo anchor, no pushed-ref check, no `files_changed` requirement.

The review workflow then issued nudges for this evidence. Reviewers (`agent_natasha`, `agent_rocky`) try to `git fetch` the branch `refs/heads/mac/agent_bullwinkle/task_d7c51a0b...-lease_38024ab9...` and get `fatal: couldn't find remote ref` (because `pushed:false`). After 10 attempts, the review is retracted with reason `reviewer_unable_to_produce_verdict_after_10_attempts`. The workflow then **creates a new review for the same evidence**, and the cycle repeats forever — 503 reviews and counting.

So the fix is layered:

1. **Reject invalid evidence at write time** for tasks the dispatcher marked as code work — `operator_result` should not be accepted for a `repo_change`-class task. See [[mem-11]].
2. **Bound review retraction** — after N retractions of the same `(task_id, executor_evidence_id)`, transition the task to a terminal failure state (or block on operator) instead of opening yet another review. See [[mem-12]].
3. **Validate the remote ref is live** when the manifest claims `pushed=true && remote_ref` — currently the validator trusts the claim, so a future executor that lies about pushing would still pass. See [[mem-13]].
4. (Original criterion still valid) **Schema-level dedupe** so a write-amplification regression can't fill the DB. See [[mem-05]].

## Acceptance Criteria

- Replay the bullwinkle path used in `task_d7c51a0b...` and assert: (a) evidence is rejected (mem-11), or (b) only one review is created before the task transitions to failed (mem-12).
- After fix, `count(task.review_claimed WHERE executor_evidence_id=?)` ≤ a bounded N (default 10).
- This ticket coordinates mem-05, mem-11, mem-12, mem-13. Close when those four are merged and verified together against a replay test.
