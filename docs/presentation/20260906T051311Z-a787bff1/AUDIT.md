# Audit — MAC v1.4.0

Audited at commit `a787bff1` on 2026-09-06T05:13:11Z. This audit supports
release `v1.4.0`. Generated CLI and OpenAPI references are authoritative
because CI verifies them against the live parser and schema.

## Current-document pass

Every current row in `docs/reference/documentation-inventory.md` was reviewed
against the candidate tree. The release range is `v1.3.5..a787bff1` (9
commits: PR #749–#756, plus a lint-format housekeeping commit).

| Documentation surface | Decision | Source anchor |
|---|---|---|
| `docs/hermes-vendor-fate.md` | changed and current: already carries a `## Update (2026-09-05)` section describing the OpenClaw→Hermes chat-gateway cutover, the filesystem root cause, and the shell-installer (not vendored, not pip) distribution model | `docs/hermes-vendor-fate.md`; PR #752, #753 |
| `docs/openclaw-identities.md` | **stale, discrepancy recorded, not fixed in this pass**: this doc describes "OpenClaw is the only provider delivery path" and details OpenClaw-specific mechanics (pinned binary inside the OpenShell sandbox). That is no longer true for any of the three fleet nodes, all now on Hermes. Rewriting this doc's OpenClaw-specific mechanics for Hermes is real scope beyond this release's code changes — filed as follow-up rather than rushed here | `docs/openclaw-identities.md` lines 1, 12, 29–31, 70; contradicted by `deploy/hermes/install-hermes-gateway.sh`, PR #752 |
| `docs/openshell-sandbox.md` | not changed: describes Hermes running *inside* OpenShell for autonomous **task execution** (`_hermes_argv` in `src/mac/task_executor.py`) — a different Hermes usage than the chat-gateway daemon this release cuts over. Not affected by PR #749–#756 | `src/mac/task_executor.py`; unchanged in this range |
| `docs/hermes-retirement-premises.md` | not changed: a dated (2026-08-04) historical findings record of the original retirement's premises. Still accurate as history; `docs/hermes-vendor-fate.md`'s update supersedes its conclusion, not its record | unchanged in this range |
| `docs/hermes-boundary.md`, `docs/hermes-integration.md` | not changed: already implementation-agnostic ("Hermes, OpenClaw, or an equivalent runtime") or scoped to the unrelated `mac-hermes` task-adapter CLI, not the chat gateway | unchanged in this range |
| `docs/reference/cli.md`, `docs/reference/openapi.md` | not changed: no CLI flag, subcommand, or HTTP route was added, removed, or renamed in this range | generated references at `a787bff1` |
| Documentation inventory | changed: this deck's own directory is a new pinned-and-allowlisted entry | `docs/reference/documentation-inventory.md` |
| All other current inventory rows | not changed by this range | source review against `v1.3.5..a787bff1` |

## Release claims

| Claim | Source |
|---|---|
| OpenClaw's colliding hourly cron jobs (`dream-cycle`, `dream-synthesis`, both `0 * * * *`) are staggered apart | `deploy/openclaw/install-openclaw-gateway.sh`; PR #749 |
| A host-side `flock` mutex serializes every sandboxed OpenClaw CLI invocation | `deploy/openclaw/run-script-cron-job.py`; PR #750 |
| OpenClaw message delivery encodes the full body through `--message` (escaped newlines), not the silently-ignored `--presentation` field | `deploy/openclaw/run-script-cron-job.py`; PR #751 |
| **Root cause identified**: OpenClaw's OpenShell sandbox state mount runs on Docker Desktop's overlayfs (VirtioFS-backed macOS VM), where POSIX advisory locking is broken enough that a fresh, empty SQLite WAL database hangs indefinitely under trivial write load — proven directly, not fixable in this repository | live reproduction, documented in `docs/hermes-vendor-fate.md`; upstream issue `github.com/openclaw/openclaw/issues/139214` |
| The fleet's chat gateway is Hermes again, not OpenClaw, on all three nodes | `docs/hermes-vendor-fate.md`; PR #752 |
| Hermes is depended on via upstream's official shell installer, never vendored and never pip-installed (upstream's own `setup.py` refuses to build a wheel or sdist) | `deploy/hermes/install-hermes-gateway.sh`; PR #752, #753 |
| Repo-owned Hermes lifecycle automation exists: `prepare`/`verify`/`finalize`/`withdraw`, matching `install-openclaw-gateway.sh`'s pattern | `deploy/hermes/install-hermes-gateway.sh`; PR #753 |
| NemoClaw (a locally-built pilot, not a third-party product) shares the same sandbox-filesystem risk class; tracked, not currently deployed on any macOS host | ledger `task_cc97f8c769aa4175b95e92d57287a128`; PR #754 |
| `hermes` is a first-class `MAC_CHAT_GATEWAY_IMPL` value in `deploy/fleet-node-install.sh`'s dispatch and `deploy-mac-fleet.sh`'s transactional orchestration | `deploy/fleet-node-install.sh`; PR #755 |
| `hermes config set` writes go to the correct dotted sub-key (`model.default`/`.provider`/`.base_url`), not the whole nested `model` object — a real regression was caught live in production (an idempotent `prepare` re-run silently discarded a working custom-router `base_url`/`api_key`) and fixed | `deploy/hermes/install-hermes-gateway.sh`; PR #756 |
| `deploy/hermes/install-hermes-gateway.sh prepare` sets `SLACK_ALLOWED_USERS=*` in `~/.hermes/.env` — a second real bug caught live: Hermes defaults every platform to `dm_policy`/`group_policy=pairing` and silently rejects any sender not on an explicit allowlist, with no startup failure, only a log line. None of the three cutover nodes had this set, meaning all three were silently rejecting every Slack message, including @mentions in the home channel. `verify` now fails closed if it is unset | `deploy/hermes/install-hermes-gateway.sh`, `tests/test_hermes_gateway_deploy.py`; confirmed applied on all three fleet nodes |

## Current measured surface

| Claim | Evidence |
|---|---|
| 9 commits since v1.3.5 | `git rev-list --count v1.3.5..a787bff1` |
| One local-only test failure (`tests/cli/test_task_table_column_widths.py::test_the_table_never_exceeds_the_terminal`), terminal-width dependent, does not reproduce in GitHub CI | local `make test` run at `a787bff1`; consistent with the same test's status noted during this session's fleet cutover work |
| `make lint`, `make docs-check` clean at `a787bff1` | local run at `a787bff1` (one formatting fix applied and committed pre-audit: `a787bff1` itself) |

## Gates and scope

This release's actual code changes are entirely in `deploy/openclaw/` and
`deploy/hermes/` (chat-gateway reliability and the OpenClaw→Hermes cutover) —
no HTTP route, CLI surface, or public Python API changed. The fleet-operational
outcome (all three nodes live on Hermes, verified healthy, `#rockyandfriends`
free-response channel policy applied identically) was executed by hand this
session and is not itself a code change; it is recorded here as ground truth
for the claims table above, cross-referenced against the actual merged PRs
that make it durable (PR #753, #755 codify what was, until they landed, a
manual and non-reproducible SSH procedure).
