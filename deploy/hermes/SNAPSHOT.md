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

## Include manifest (runtime surface we own)

Vendor these into `src/mac/_hermes/` (paths relative to the upstream checkout):

- `agent/`            — agent loop, adapters, context engine (~68k LOC)
- `gateway/`          — Slack/Telegram/Discord gateway, session, run (~82k LOC)
- `providers/`        — provider registry (~0.4k LOC)
- `hermes_cli/`       — runtime modules actually imported by agent/gateway,
                        notably `runtime_provider.py` (the function the deleted
                        string-surgery shim used to call). Prune CLI-only
                        sub-trees not reachable from the gateway/agent runtime.
- `skills/`, `tools/` — only the entries actually invoked by deployed agents;
                        audit with an import/usage trace before vendoring whole.
- `hermes`, `cli.py`, `hermes_*.py` top-level runtime entry shims as needed.

## Exclude manifest (cruft — do NOT vendor)

- `website/`               (731 files)
- `ui-tui/`                (344 files)
- `web/`                   (97 files)
- `infographic/`, `assets/`, `locales/`
- `datagen-config-examples/`, datagen/training scaffolding
- `docs/` (keep only runtime-relevant docs, if any)
- upstream `tests/` (port only the tests covering vendored runtime code)

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
