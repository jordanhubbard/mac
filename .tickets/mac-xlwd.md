---
id: mac-xlwd
status: closed
deps: []
links: []
created: 2026-05-28T03:28:51Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-xlwd
---
# Wire auto-publish hook so APPROVED reviews actually merge to main

Root cause of 'no autonomous merge has ever landed on main'. The merge code at services.py:6629-6755 is correct and complete: it fast-forwards or creates a merge commit, then runs git push origin main (line 6732). The problem is nothing calls it.

publish_task() requires explicit invocation via POST /publications or `mac-hermes publish <task_id> <target> <evidence_id>`. Worker.run_once polls for new tasks and review nudges (worker.py:687-723) but never looks back at completed reviews to ask 'should I publish this?' Hermes likewise has no post-approval workflow that triggers publish.

Empirically: 161 tasks marked 'completed' in rocky's ledger, 0 of them resulted in code landing on a project main branch. 8 publication records exist with target=git://main; 7 of them point at per-lease scratch branches on a private MAC git mirror, not GitHub. The 1 that does name refs/heads/main was authored by Jordan via the operator-reconciler human shim.

Best-case autonomous run: task_a0fd466c34d941719d08f8540fa78833 (2026-05-23). Worker did the work, reviewer approved, publication record was tagged git://main — but git was never touched on the real main branch.

Action: add a post-approval trigger that, on any review submit_review() transitioning to APPROVED with target=git://main, automatically invokes publish_task() with the approved evidence_id. Options:
1. Inline in review_service.submit_review(): when status flips to APPROVED and review.target == 'git://main', call publish_task() in the same transaction (idempotent: skip if a successful publication already exists).
2. Post-commit hook / outbox: enqueue a 'try_publish' job in the same outbox the dispatcher drains.
3. Worker idle loop: have idle workers poll for 'task in COMPLETED state with approved review and no publication' and try to publish.

Option 1 is the simplest and matches the existing _sync_beads_close() pattern in review_service (publish-then-close happens inside the publish flow; the inverse — close-then-publish — should be just as automatic).

## Acceptance Criteria

- A review approval whose target is git://main automatically triggers publish_task() against the approved evidence_id
- The trigger is idempotent: re-running submit_review on an already-published task is a no-op
- A new contract test exercises: create task -> claim -> evidence -> submit-for-review -> approve -> assert publication record exists AND git main contains the reviewed SHA
- Test runs against a temporary git repo; no human in the loop

## Close Reason

Superseded by empirical finding: the auto-publish hook ALREADY exists (services.py:7180, advance_default_review_workflow). It fires automatically during dispatch ticks, calls _publish_git_target_if_needed, which performs git fetch/merge/push origin main. The hook has fired 8 times and produced 3 successful autonomous-agent commits on jordanhubbard/mac main (44b2753, 072bfa9, 2658a6c) — all docs:lifecycle-proof receipts. The remaining 5 went to a private MAC mirror because the worktree's origin is wrong, not because the hook is missing. Real first-mover is mac-y7ha (validate worktree origin against registered project remote). Promoting it to P0 and unblocking mac-8c0z.
