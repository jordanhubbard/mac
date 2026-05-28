---
id: mac-8c0z
status: open
deps: [mac-xlwd]
links: []
created: 2026-05-28T03:29:00Z
type: bug
priority: 1
mac-task-id: pending:mac-8c0z
---
# Validate publication target matches evidence remote_ref

Publication records can be tagged target=git://main while their evidence's verification.repo.remote_ref is a lease branch like refs/heads/mac/agent_rocky/task_*-lease_*. MAC marks the task 'completed' anyway, decoupling bookkeeping from real git state.

Concrete instance: 7 of the 8 git://main publications in rocky's ledger have evidence whose remote_ref is the lease branch, not main. From the outside the task looks 'merged'; reality: git was never touched.

Action: in _publish_git_target_if_needed (services.py:6629-6755) — before doing any git ops — validate that:
- If target startswith 'git://', the parsed branch must equal evidence.verification.repo.remote_ref minus the 'refs/heads/' prefix
- OR the publish flow itself must update the evidence's remote_ref after a successful push (the post-merge SHA verification at lines 6735-6744 could write back)

Prefer the validation path: reject publications where the source branch in evidence doesn't match target, with a clear error. The publish flow should not silently 'succeed' on a lease branch.

Related to mac-{auto-publish} — without that wired up, this check would still catch lease-branch publishes from any human invocation as well.

## Acceptance Criteria

- publish_task() with target=git://main and evidence.repo.remote_ref != refs/heads/main raises ValidationError before any git op
- Existing 'publish to lease branch' code paths in tests are updated to reflect the new contract
- Audit of the rocky ledger shows zero new git://main publications with mismatched remote_ref after the fix is deployed
