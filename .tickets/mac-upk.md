---
id: mac-upk
status: closed
deps: []
links: []
created: 2026-05-20T05:52:21Z
type: task
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-upk
---
# Lift repo.head_sha+pushed gate into a shared helper called by every evidence_type branch

Refactor target driven by the umbrella git-anchor decision.

`_repo_verification_problems` (services.py:2759-2786) currently runs only for `repo_change` and `documentation`. Every other branch of `_verification_type_problems` (test, artifact, deployment, no_change) has its own ad-hoc check list that omits the repo gate entirely.

Goal:
- Extract `_require_pushed_repo_anchor(manifest) -> List[str]` that validates verification.repo.head_sha is a 40-char git SHA, dirty=false, pushed=true with remote_ref (or pr_url for repo_change).
- Every branch of `_verification_type_problems` (including `review_verdict` validation in `_find_review_verdict_evidence`) calls this helper FIRST, then adds its type-specific additional requirements (e.g., test still needs a passing check; deployment still needs targets).
- Single doorway. Single failure mode. Single mock target for tests.

This is the structural cleanup that lets the per-type P0s above land without copy-paste drift between branches.

Depends on the umbrella anchor decision but is the implementation vehicle for all four — file as the consolidation issue.

## Close Reason

Added shared pushed repo anchor validation across evidence types, enforced review readiness before needs_review, required verifiable review verdicts before publication, and added worker-side verification failure handling with regression tests.
