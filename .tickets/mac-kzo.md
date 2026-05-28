---
id: mac-kzo
status: closed
deps: [mac-upk]
links: []
created: 2026-05-20T05:52:13Z
type: bug
priority: 0
assignee: agent_natasha
mac-task-id: pending:mac-kzo
---
# review_verdict has no git anchor and no proof reviewer re-ran the work

The `review_verdict` manifest landed in mac-jqb (services.py:2865+) carries:
- `verdict`: approved | rejected
- `reviewed_evidence_id`: pointer to the executor's evidence row
- `signed_by` / `signature`: reviewer's HMAC

It does NOT carry:
- `verification.repo.head_sha` — what commit was reviewed
- `verification.repo.pushed` — whether that commit is on the remote (so the reviewer fetched something witnessable)
- `tests[]` — the reviewer's independent re-run against the executor's pushed SHA
- a worktree checksum at that SHA

Failure modes:
1. A reviewer can sign approval for an executor's evidence whose verification.repo points at a commit that was rewritten or never pushed. The reviewer never proves they inspected the same artifact that the publish step will key off.
2. The reviewer can approve without running anything. mac-din was supposed to pin 'reviewer did independent work' — today we only pin 'reviewer signed something'. A signature is not work.
3. There is no record of WHAT the reviewer reviewed. The audit trail says 'approved' but not 'approved against SHA X with test results Y'.

Fix:
- review_verdict manifest schema additions:
  - `verification.repo.head_sha` (REQUIRED): the SHA the reviewer fetched + inspected. MUST equal the executor's evidence repo.head_sha (or be a successor in a stacked-diff scenario).
  - `verification.repo.pushed=true` + `remote_ref` (REQUIRED): the reviewer must have fetched from the remote, not a local copy.
  - `tests[]` (REQUIRED for repo_change/test/artifact/deployment review_verdicts): reviewer's independent re-run, with at least 1 returncode=0 entry.
  - `worktree_digest` (REQUIRED): sha256 of `git ls-tree -r HEAD` at the reviewed SHA — proves the reviewer had the same bytes as the executor.
- `_find_review_verdict_evidence` validates these fields before treating the verdict as binding.

Acceptance:
- `test_review_verdict_rejected_without_repo_anchor`
- `test_review_verdict_rejected_when_repo_sha_disagrees_with_executor`
- `test_review_verdict_rejected_when_worktree_digest_disagrees`
- `test_review_verdict_rejected_when_reviewer_tests_all_failed`

This is the natural follow-up to mac-jqb. mac-jqb pinned 'reviewer must sign something'; this pins 'reviewer must do something git-witnessable to sign.'

## Notes

mac task task_c0f9c247b5a34ec18abe7e52a164aba8 failed: beads_failed_task_retry_limit
mac task task_c0f9c247b5a34ec18abe7e52a164aba8 failed: beads_failed_task_retry_limit
mac task task_c0f9c247b5a34ec18abe7e52a164aba8 failed: beads_failed_task_retry_limit

## Close Reason

Implemented and verified with focused tests: review verdict git anchoring/independent check coverage and fleet deploy active-worker drain behavior.
