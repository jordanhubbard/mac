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
  imports), so the static trace shows it unreached.

### Runtime trace result (validates the static manifest)

A real oneshot (`hermes -z` under `python -v`) on hosta confirmed:
- **`plugins/` loads 273 files at runtime** (vs 20 statically — dynamic
  loading). It is essential; omitting it would break the vendor.
- `hermes_cli` (87), `agent` (78), `gateway` (27), `tools` (15), `providers`
  (6) + top-level entry modules all load — subset of the static set, since a
  trivial oneshot doesn't exercise every path. **Vendor the union of static ∪
  runtime** (the INCLUDE arrays already cover every reachable subtree).
- **`skills/` did NOT load** in a basic oneshot — agent skills are likely
  loaded on-demand or from `HERMES_HOME`, not the checkout. Vendor
  conservatively, but confirm whether the checkout `skills/` is needed at all
  before treating it as required.

## Exclude manifest (cruft — do NOT vendor) — MEASURED

Top-level dirs **never statically reached** from the runtime entrypoints:

- `website/` (731), `ui-tui/` (344), `web/` (97)
- `infographic/`, `assets/`, `locales/`
- `datagen-config-examples/`, `docs/`, `docker/`, `nix/`, `packaging/`
- `acp_registry/`, `optional-mcps/`, `optional-skills/` (verify per above)
- `plans/`, `scripts/`, upstream `tests/`

## Vendor strategy: sys.path injection, NOT namespace rewrite

Measured fact: Hermes is a **flat top-level package layout** and imports its own
code with **top-level absolute imports** everywhere (`from agent import …`,
`from gateway import …`, `from hermes_cli/tools/plugins/providers import …`,
`from hermes_constants import …` — hundreds of references; no `from ..` parent-
relative imports). It is installed via `setuptools.packages.find`.

Therefore the vendor needs **no import rewriting**. Vendor the tree into
`src/mac/_hermes/` and put that directory on `sys.path` (the vendor stamps a
bootstrap; mac inserts it before importing the gateway in-process). Then
`agent`, `gateway`, `hermes_cli`, `tools`, `plugins`, `providers`,
`hermes_constants`, etc. all resolve from the vendored tree unchanged. This is
what makes hu-03 (in-process gateway) tractable: the hard parts are the
dependency-manifest merge and the in-process launch, not 350k LOC of namespace
surgery. Validate by importing `gateway.run` from the vendored path once deps
are merged.

## Dependency-manifest merge — MEASURED, no conflicts

Hermes core deps: 16 exact pins. mac deps: 7 (ranges). **Only 2 overlap, both
compatible:**
- `httpx[socks]==0.28.1` ⊂ mac `httpx>=0.27,<1.0` → adopt Hermes pin (+`[socks]`)
- `pyyaml==6.0.3` ⊂ mac `PyYAML>=6.0,<7.0` → adopt Hermes pin

Merge = mac's deps (fastapi, uvicorn[standard], cryptography, slack-sdk) + the
two tightened overlaps + Hermes's 14 unique core pins: `openai==2.24.0`,
`pydantic==2.13.4`, `prompt_toolkit==3.0.52`, `rich==14.3.3`, `tenacity==9.1.4`,
`jinja2==3.1.6`, `requests==2.33.0`, `fire==0.7.1`, `croniter==6.0.0`,
`psutil==7.2.2`, `PyJWT[crypto]==2.12.1`, `python-dotenv==1.2.2`,
`ruamel.yaml==0.18.17`, `tzdata==2025.3; sys_platform=='win32'`. Bump
`requires-python` to `>=3.11` (Hermes requirement; fleet venvs are already 3.12).
Hermes's provider-specific extras (anthropic, firecrawl-py, exa-py, …) stay
**lazy** via `tools/lazy_deps.py` — do not pull into core (supply-chain blast
radius). Adopt Hermes's exact-pin discipline going forward.

## Patches to fold in permanently (then delete the .patch files)

These out-of-tree patches become ordinary in-tree edits once vendored:

- `disable-shutdown-chat-notices.patch` → `gateway/run.py`
- `mac-runtime-context-prompt.patch`    → `agent/prompt_builder.py`
- `multi-slack-mvp.patch` (1,372 lines) → `gateway/platforms/slack.py`,
                                           `gateway/session.py`
- `mac-provider-decision.patch`         → `gateway/run.py` — hu-03: the owned,
  in-process replacement for the string-surgery provider/model shim. The
  vendored gateway calls `mac.agent_provider` directly (no-op standalone).

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
