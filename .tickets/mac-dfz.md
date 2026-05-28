---
id: mac-dfz
status: closed
deps: [mac-upk]
links: []
created: 2026-05-20T05:51:50Z
type: bug
priority: 0
mac-task-id: pending:mac-dfz
---
# Verification taxonomy under-couples to git: test/artifact/deployment/no_change pass review without any commit

The default-review evidence taxonomy (services.py:2711-2786 `_verification_type_problems`) requires `verification.repo.head_sha` only for `repo_change` and `documentation`. The other four evidence types — `test`, `artifact`, `deployment`, `no_change` — all pass the review gate without any git anchor:

- `test` / `artifact`: only needs 1 passing check; no repo block.
- `deployment`: only needs 1 passing check + targets/services/artifacts; no repo block.
- `no_change`: only needs `reason` + 1 passing check; no repo block.

Failure mode in an autonomous swarm: an agent submits `evidence_type=test` with returncode=0 and a fabricated test name. The default-review workflow approves and publishes. There is no third-party witness that any code actually exists at a knowable state. The git remote — the only signed, externally-witnessed, durable record — is bypassed entirely.

Fix: require `verification.repo.head_sha` (40-char git SHA matching `_GIT_SHA_RE`) plus `pushed=true` with `remote_ref` for ALL evidence types. The pushed commit is the only proof the work isn't stranded locally. Specifically:
- `test`: require repo.head_sha + pushed; the tests must declare what commit they ran against.
- `artifact`: require repo.head_sha + pushed; the artifact must declare its source commit.
- `deployment`: require repo.head_sha + pushed; the deployment must declare what was deployed.
- `no_change`: require repo.head_sha + pushed; the agent inspected a specific pushed commit and concluded no change needed (HEAD of main is fine — the point is the commit must exist and be on the remote).

Existing repo.head_sha enforcement in `_repo_verification_problems` (services.py:2759-2786) can be lifted out into a shared helper called by every branch of `_verification_type_problems`.

Acceptance: a test like `test_<each_type>_evidence_rejected_without_pushed_head_sha` exists for each of `test`, `artifact`, `deployment`, `no_change` — proving the default-review workflow refuses them when verification.repo is missing or unpushed.

## Notes

User framing (2026-05-19): 'If a completed task is not the result of a git commit, a review of that commit with follow-on changes (stacked diff), and QA (testing) of that commit, then the commit is actually pushed and not stranded, then what is it the result of?' — the answer today is 'the agent's self-report,' which is exactly the failure mode mac-ng2 closed for repo_change manifests but left open for everything else.

## Close Reason

All non-repo evidence types now call the shared pushed repo anchor gate before type-specific checks; added parametrized review-readiness coverage for test, artifact, deployment, no_change, and documentation.
