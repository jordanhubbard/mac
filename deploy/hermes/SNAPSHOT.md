# Hermes runtime snapshot pin

This file is the contract for the vendored, **owned** snapshot of the Hermes
agent runtime that ADR 0001 (`docs/adr/0001-unify-hermes-runtime-into-mac.md`)
moves us toward. It replaces the old deploy behavior of cloning pristine
`NousResearch/hermes-agent` and applying patches + runtime string surgery.

## Current pin

| Field | Value |
| --- | --- |
| Upstream | `https://github.com/NousResearch/hermes-agent.git` |
| Commit | `b1a25404b638bfbd79ce4d08b49afc0ee1361528` |
| Upstream version | `0.15.1` |
| Captured | 2026-05-30 |
| Vendored into repo | **not yet** (stage 2 — see ADR migration plan) |

Bumping the pin is a deliberate, reviewed act. We do **not** track the upstream
commit-of-the-day. "Mature" means pinned.

## Include manifest (runtime surface we own) — MEASURED

Derived from `scripts/trace-hermes-reachability.py` (static import closure from
the deployed gateway + oneshot-agent entrypoints). **394 reachable first-party
files.** Reachable subtrees, with file counts:

- `hermes_cli/` (109) — runtime modules imported by agent/gateway, notably
  `runtime_provider.py` (the function the deleted string-surgery shim called)
- `agent/` (103) — agent loop, adapters, context engine
- `tools/` (86) — tool implementations reachable from the runtime
- `gateway/` (50) — Slack/Telegram/Discord gateway, session, run
- `plugins/` (20) — **reachable; was missing from the first guess — must vendor**
- `acp_adapter/` (8), `cron/` (3), `tui_gateway/` (2), `providers/` (2)
- top-level entry modules: `hermes_bootstrap.py`, `cli.py`, `mcp_serve.py`,
  `run_agent.py`, `model_tools.py`, `toolsets.py`, `utils.py`,
  `hermes_time.py`, `hermes_state.py`, `hermes_logging.py`, `hermes_constants.py`
- `hermes` (the launcher script)

**Runtime-loaded, NOT statically imported (vendor anyway, verify at runtime):**
- `skills/` — markdown/skill library the agent scans at runtime (data, not
  imports), so the static trace shows it unreached. Confirm with a runtime
  import/usage trace (gateway boot + oneshot under `-X importtime`) before
  trusting the static set; also re-check `optional-skills/` / `optional-mcps/`
  and `tools/lazy_deps.py` lazy paths.

## Exclude manifest (cruft — do NOT vendor) — MEASURED

Top-level dirs **never statically reached** from the runtime entrypoints:

- `website/` (731), `ui-tui/` (344), `web/` (97)
- `infographic/`, `assets/`, `locales/`
- `datagen-config-examples/`, `docs/`, `docker/`, `nix/`, `packaging/`
- `acp_registry/`, `optional-mcps/`, `optional-skills/` (verify per above)
- `plans/`, `scripts/`, upstream `tests/`

## Patches to fold in permanently (then delete the .patch files)

These out-of-tree patches become ordinary in-tree edits once vendored:

- `disable-shutdown-chat-notices.patch` → `gateway/run.py`
- `mac-runtime-context-prompt.patch`    → `agent/prompt_builder.py`
- `multi-slack-mvp.patch` (1,372 lines) → `gateway/platforms/slack.py`,
                                           `gateway/session.py`

## Runtime string surgery to delete (replaced by owned code)

`src/mac/hermes_startup.py` rewrites upstream source at runtime. After
vendoring, delete these in favor of owned, in-process code:

- `_apply_gateway_runtime_shim` (gateway model/provider override)
  → already replaced in intent by `src/mac/agent_provider.py`; the vendored
    `gateway/run.py` should call `mac.agent_provider.resolve_agent_provider`
    directly.
- `_maybe_apply_slack_account_activation_shim`, `_slack_home_channel` shims
  → fold the behavior into the vendored gateway directly.

## How to (re)snapshot

```bash
scripts/vendor-hermes-snapshot.sh            # uses the pin above
scripts/vendor-hermes-snapshot.sh <commit>   # bump the pin (review the diff!)
```
</content>
