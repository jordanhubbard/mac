---
id: mac-y7ha
status: closed
deps: [mac-xlwd]
links: []
created: 2026-05-28T03:29:09Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-y7ha
---
# Validate publication target matches the registered project's remote_url

Publication records can succeed against a target like git://main while operating on a private MAC git mirror at ssh://jkh@100.125.137.89/home/jkh/.mac/git/c26.git — not the project's registered GitHub remote. A 'merge to main' that pushes to a private mirror is not what the publication record claims.

Currently the publish flow (services.py:6629-6755) walks the local worktree, derives origin from `git config remote.origin.url`, and pushes to whatever that is. If the worktree was cloned from the MAC mirror instead of GitHub, the push lands on the mirror and the publication is recorded as successful.

Action: when a task is associated with a registered project (via the bridge or beads_repositories table), publish_task must:
1. Fetch the project's canonical remote URL from the registry (bridge table or beads_bridge_service).
2. Verify the worktree's origin matches that URL (allow http<->ssh URL equivalence rewrites).
3. Reject the publish with a clear error if they differ.

This is independent of mac-{auto-publish} and mac-{target-remote_ref} — a publish call could pass both of those checks and still land on a mirror. Defense in depth.

## Acceptance Criteria

- publish_task() against a project-bound task fails fast if worktree origin != project registered remote_url
- Tests cover: same URL (pass), http<->ssh equivalent (pass), wrong host (fail), private mirror URL (fail)
- The error message identifies both the worktree origin and the expected project remote

## Notes

2026-05-27 implementation landed (local; not yet committed):
- Added _canonicalize_git_url() helper in services.py:303-336 — canonicalizes git URLs to (host, path) tuple, supports ssh://, git@host:path, https://, git:// forms, strips trailing .git, lowercases host.
- Extended _normalize_repository_contract (services.py:440-453) to accept optional canonical_remote_url field. Schema-validates parseability on registration so a typo at config time surfaces immediately.
- Added origin-mismatch guard in _publish_git_target_if_needed (services.py:6717-6747): reads task.metadata.origin.repository_contract.canonical_remote_url, probes worktree's origin via `git remote get-url origin`, compares canonical forms, raises ValidationError on mismatch.
- Updated this repo's .mac/project.yaml to opt-in with canonical_remote_url: git@github.com:jordanhubbard/mac.git.
- Three new tests in test_control_plane.py: test_git_publication_rejects_worktree_origin_mismatch, test_git_publication_accepts_equivalent_ssh_https_origin, test_git_publication_skips_origin_check_when_contract_unset. All three pass. Full test_control_plane suite (187 tests) passes. Full suite (541 tests outside the unrelated mac-agent-console-script E2E) passes.

Back-compat: contracts without canonical_remote_url retain prior (unvalidated) behavior. Existing rocky bridges (mac, c26, nanolang) won't change behavior until operator adds canonical_remote_url to their .mac/project.yaml.

Operational follow-up: c26's beads_repository on rocky needs canonical_remote_url + worktree re-pointed at GitHub. nanolang likewise. Without that, 7/8 historical "publish to git://main" lease-branch-to-private-mirror behavior continues. After this PR lands, those repos will simply raise a clear error when an operator wires the contract.

## Close Reason

Implemented and pushed in commit c9f1aa4. Added _canonicalize_git_url() helper, extended repository_contract schema to accept canonical_remote_url, added origin-mismatch guard in _publish_git_target_if_needed, opted this repo in via .mac/project.yaml. Three new tests pass; full test_control_plane suite (187 tests) and broader suite (541 tests) pass.
