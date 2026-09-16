# Audit — MAC v1.3.5

Audited at commit `c7a3fee1060e1d0a4b1a968f597f0e6090a8efc9` on
2026-09-04T21:25:15Z. This audit supports release `v1.3.5`. Generated CLI and
OpenAPI references are authoritative because CI verifies them against the live
parser and schema.

## Current-document pass

Every current row in `docs/reference/documentation-inventory.md` was reviewed
against the candidate tree. The release range is `v1.3.4..c7a3fee1` (38
commits), which supersedes the `v1.3.4..a168e9d0` range already audited in
`docs/presentation/20260902T131314Z-a168e9d0/AUDIT.md` and adds the commits
below.

| Documentation surface | Decision | Source anchor |
|---|---|---|
| `docs/openshell-sandbox.md` | not changed: the OpenShell onboarding fixes in this range are internal bootstrap/runtime-verification behavior, not a documented CLI or contract surface | `deploy/openshell/bootstrap-openshell.sh`; PR #740, #743, #744 |
| `docs/dispatch-priority-bias-audit.md` | not changed: the targeted-dispatch fix corrects a bug in reaching the documented priority ordering, it does not change the ordering itself | `src/mac/task_lifecycle.py`; PR #745 |
| `docs/reference/cli.md`, `docs/reference/openapi.md` | not changed: no CLI flag, subcommand, or HTTP route was added, removed, or renamed in this range | generated references at `c7a3fee1` |
| Documentation inventory | changed: this deck's own directory is a new pinned-and-allowlisted entry | `docs/reference/documentation-inventory.md` |
| All other current inventory rows | not changed by this range | source review against `v1.3.4..c7a3fee1`; no corresponding documentation diff |

No README/code discrepancy was found in this range: none of PR #737–#747
touched a README-documented behavior, only internal onboarding, dispatch, and
attestation mechanics with no external contract.

Nine ADRs remain `Status: **Proposed**` in this tree (`0002`, `0003`, `0005`,
`0006`, `0017`, `0018`, `0020`, `0021`, `0022`); all predate this release
range and none is presented as shipped in this deck.

## Release claims

| Claim | Source |
|---|---|
| OpenShell reviewed-CLI preflight computes full identity whenever a canonical CLI binary already exists on disk, not only when OpenClaw itself is sandbox-managed | `deploy/openshell/reviewed-cli.py`; PR #737 |
| Fleet-node install tolerates an absent `service-advertisement.json` under a degraded gateway instead of crashing the OpenClaw sandbox-conformance check | `deploy/fleet-node-install.sh`; PR #737 |
| The OpenClaw gateway installer's "no such process" detection matches by message text, independent of `supervisorctl`'s exit code | `deploy/openclaw/install-openclaw-gateway.sh`; PR #737 |
| Sandboxed agents are no longer advertised a host-absolute alternative to `$MAC_TASK_REPO_WORKTREE` | `src/mac/executor_prompt.py`; PR #738 |
| README no longer leaks fleet agent names via canary checkpoint comments | `README.md`; PR #739 |
| OpenShell bootstrap polls for local gateway readiness (up to 120s) instead of a fixed 3-second sleep, fixing cold-image-pull races on brand-new nodes | `deploy/openshell/bootstrap-openshell.sh`; PR #740 |
| `retain_forward` node recovery reconciles the bound worker's attestation authority against the correct hub before releasing its recovery lock | `deploy/deploy-mac-fleet.sh`; PR #741 |
| `OpenShellService.agent_status` requires a live `sandbox_id`, not just `status=="active"`, before reporting a deployed/fail-open sandbox | `src/mac/openshell_service.py`; PR #742 |
| `OPENSHELL_GATEWAY_ENDPOINT` is pinned into `mac.env` at bootstrap, making mac-agent immune to ambient `openshell gateway select` drift | `deploy/openshell/bootstrap-openshell.sh`; PR #743 |
| The `openshell-sandbox` binary is verified statically linked (no ELF `PT_INTERP`) before install, eliminating host/container glibc mismatches | `deploy/openshell/bootstrap-openshell.sh`; PR #744 |
| An agent with an explicitly `target_agent_id`-scoped open task claims it directly instead of only being reachable through the global dispatch round | `src/mac/task_lifecycle.py`; PR #745 |
| The targeted-dispatch fix has diff coverage meeting the 80%/50% statement/branch floor | `tests/test_dispatch_service_v2.py`; PR #746 |
| Eight files touched across PRs #741–#746 are `ruff format`-clean | housekeeping; PR #747 |

## Current measured surface

| Claim | Evidence |
|---|---|
| 433 HTTP routes | generated `docs/reference/openapi.md` at `c7a3fee1` |
| Generated CLI reference has six top-level command groups | generated `docs/reference/cli.md` at `c7a3fee1` |
| 38 commits since v1.3.4 | `git rev-list --count v1.3.4..c7a3fee1` |
| Nine ADRs remain Proposed, none shipped in this range | status-line scan of `docs/adr/` at `c7a3fee1` |

## Gates and scope

- Candidate commit: `c7a3fee1060e1d0a4b1a968f597f0e6090a8efc9` (main, includes PR #747).
- `make lint`: clean after PR #747 (ruff format drift on 8 files fixed).
- `make test` (local, 8-way parallel, 11,438 collected): 2 failures, both
  confirmed environment-local artifacts unrelated to any change in this
  range — `test_the_table_never_exceeds_the_terminal` depends on the actual
  host terminal width, and `test_invoke_unsandboxed_uses_private_prompt_wrapper`
  depends on this checkout's local venv binary being named `python3` rather
  than `python`. GitHub Actions CI (`mainline`/PR gates), which does not
  share this host's terminal or venv naming, is green on this commit's
  ancestor `0002167a` and on PR #747 itself.
- `make docs-check`: clean.
- Two additional pre-existing, order-dependent CI flakes were observed
  post-merge on `main` earlier in this range (a `launchd`-bootout timing
  test and a `review-tick`-orchestrator test-isolation leak in the
  `portfolio` job) and were judged non-blocking, consistent with the
  continuously-recurring "main is red" issue #290 that did not block v1.3.4.
- Fleet cutover and image qualification are out of scope for this deck; see
  `docs/synchronized-fleet-cutover.md` and
  `docs/image-publication-and-qualification.md`.
