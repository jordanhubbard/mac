---
id: repo-onboard-01
status: open
deps: []
links: []
created: 2026-06-05T00:00:00Z
type: feature
priority: 2
audit: repository-onboarding-followup
discovered_via: onboarding_fix
---
# `mac repository onboard <url>` CLI command

## Why this exists

Repository onboarding now works end-to-end via the service method
`ControlPlane.onboard_repository(url, ...)` and the `POST /repositories/onboard`
API endpoint. These produce the contract-backed onboarding task
(`origin.type=direct_task` + `repository_url` + `onboarding=true`) that lets a
worker clone a task-owned worktree and author `.mac/project.yaml` — the contract
being the onboarding task's *output*, not a precondition.

The **CLI sugar was deferred**: `mac repository onboard <url> --project <name>`
is not wired because the `mac` CLI reaches a hub through `HubClient`
(`src/mac/http_client.py`), which has no generic method→endpoint proxy — each
operation must be added explicitly. Operators currently onboard via the API
(`POST /repositories/onboard`) or `cp.onboard_repository` in local-db mode.

## Acceptance Criteria

- `mac repository onboard <url> [--project <name>] [--default-branch <ref>]
  [--title <t>]` creates the onboarding task against the resolved hub.
- Works in hub mode (add `HubClient.onboard_repository` → `POST
  /repositories/onboard`) and local-db mode (`cp.onboard_repository`).
- Unit test mirrors `tests/test_repository_onboarding.py` for the CLI dispatch
  path, plus a `tests/cli/` smoke if the CLI test harness covers it.

## Notes

- Shipped in the same change (main `dd8b659`): the onboarding-aware executor
  prompt — `task_executor.repository_contract_section` 3-way (show contract /
  ONBOARDING guidance when a checkout exists but no contract / genuine
  contract-failure only when no repo) + an explicit autonomy instruction in
  `build_task_prompt`.
- Verified live on the `jordanh-gke` fleet: onboarding `NVIDIA-dev/taskbrain`
  produced a valid authored `.mac/project.yaml` (`mac.repository_contract.v1`),
  an architecture summary, and a 10-item backlog (`task_c93363c1…`,
  `needs_review`) — versus the prior 12s "please confirm" non-result.
