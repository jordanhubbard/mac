---
id: mac-zpku
status: open
deps: []
links: []
created: 2026-05-28T17:32:22Z
type: bug
priority: 0
mac-task-id: pending:mac-zpku
---
# bullwinkle (macOS) worker: GitHub lease-branch push fails silently — evidence.repo.pushed=False

During the mac-wsny e2e proof, bullwinkle (the macOS worker on jordanh's local Mac) ran a task end-to-end (file edit + local commit, head_sha=06b4a1423ddcd92af2684236225c6fbdc38eb031) but the evidence shows verification.repo.pushed=False. The lease branch refs/heads/mac/agent_bullwinkle/task_e5043d35a817498db46e14b09286f21b-lease_7ce284b6d54a4b3da8 does not exist on the GitHub remote (git ls-remote returns empty for it), while v1 task_56c6aeea51584f638814d0d7b052034e DID push successfully (lease branch present on origin with head_sha 7076b5d14c91...).

Suspected cause: bullwinkle's worktree origin is git@github.com:jordanhubbard/mac.git (SSH), but bullwinkle does not have the same GitHub review deploy key configured as rocky/natasha (the deploy script installs ~/.ssh/mac_github_review_id on the Linux hosts only). When the worker subprocess attempts `git push`, the SSH negotiation falls back to the user's default keys, which may or may not work depending on local agent state.

Action:
1. Verify whether the Linux-only deploy step that installs the GitHub deploy key (deploy-mac-fleet.sh: "installed GitHub review deploy key at /home/jkh/.ssh/mac_github_review_id") also runs on darwin targets. If not, port it.
2. After ensuring the key is present on bullwinkle, retest e2e and verify lease branch lands on origin.
3. Make worker push failures emit verification.problems instead of silently writing pushed=False — currently the evidence looks complete and only the reviewer notices the missing ref much later.

Discovered during attempt to demonstrate autonomous-merge for mac-wsny.

## Acceptance Criteria

- bullwinkle's mac-hermes-task-executor can `git push origin <lease-branch>` against git@github.com:jordanhubbard/mac.git without manual auth intervention
- A subsequent e2e task run by bullwinkle produces evidence with verification.repo.pushed=True and the branch exists on origin
- Push failures generate verification.problems entries, not silent pushed=False
