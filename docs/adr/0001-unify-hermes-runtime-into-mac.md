# ADR 0001 — Unify the Hermes runtime into the `mac` monorepo

- Status: **Superseded (vendoring premise ended 2026-08-17)**
- Date: 2026-05-30
- Decision owner: Jordan Hubbard
- Context: validating the premise that the three-month "circling the drain"
  is caused by `mac`, `hermes-agent`, and `tokenhub` being separate systems
  with separate event loops and opaque boundaries — and that folding a
  mature snapshot of `hermes-agent` into `mac` would make the fleet easier
  to observe, coordinate, and keep interactive.

## Amendment — 2026-08-17

ADR 0001 accepted vendoring a pruned Hermes runtime into `src/mac/_hermes/`
and running it **in-process**. That premise ended when the live fleet
converged on OpenClaw as the only active gateway
(`gateway_ownership.services = {hermes: inactive, nemoclaw: inactive,
openclaw: active}` per `docs/hermes-retirement-premises.md`).

PR #377 removed the inactive snapshot (~444k lines), `hermes_vendor.py`,
`hermes_gateway.py`, `deploy/hermes/` (including `SNAPSHOT.md` and the
re-vendor tooling), the hermes-revendor CI job, and the sandbox `.pth`
injection. Hermes can still be fetched and patched on demand; full git
history retains every byte. See `docs/hermes-vendor-fate.md` for the
pre-deletion checks (a)–(d) and the post-removal inventory.

Residual (out of scope for the fate decision): a legacy Hermes gateway
install path in `deploy/fleet-node-install.sh` still references
`python -m mac.hermes_gateway` (module absent). It is inert on the OpenClaw
fleet and belongs in a separate deploy cleanup.

The body below is the original accepted decision and remains historical
context for why the tree was once carried in-tree.

## TL;DR verdict

The premise is **two-thirds right, and the right two-thirds matter most.**

| Boundary | Is it a root cause? | Decision |
| --- | --- | --- |
| **mac ↔ hermes-agent** | **Yes.** | **Merge.** Own a pinned, pruned, in-tree snapshot of the Hermes *runtime*; run it in one process under mac's event loop; delete the runtime string-surgery shims; fold the out-of-tree patches in-tree. |
| **mac ↔ tokenhub** | **No** (not in the way the premise implies). | **Keep separate.** It is mature Go infra (Thompson-sampling router, encrypted vault, Temporal). The "dark spot" is an *observability gap*, not a boundary problem. Consume tokenhub's existing decision feed instead of rewriting it. |
| **mac's internal pain** (inert memory tier, 3.1 GB observability table, runaway loops) | **No.** | These are mac-internal bugs/ops gaps. Merging Hermes does not fix them; they have their own tickets and partial fixes already landed. |

"Single coherent codebase" is the correct instinct **for the agent runtime**.
It is the *wrong* instinct for tokenhub, and it is *irrelevant* to mac's
own internal bugs. Conflating all three is how the last two efforts
(openclaw, ACC) became "too complicated."

## What is actually true today (ground truth, not assumptions)

### 1. mac does not own Hermes — it patches a pristine, fast-moving upstream clone

`deploy/deploy-mac-fleet.sh:3995` runs:

```console
git clone --quiet https://github.com/NousResearch/hermes-agent.git "$HERMES_DIR"
```

It then mutates that clone three ways:

1. **Three out-of-tree git patches** in `deploy/hermes/`:
   - `disable-shutdown-chat-notices.patch` (15 lines → `gateway/run.py`)
   - `mac-runtime-context-prompt.patch` (63 lines → `agent/prompt_builder.py`)
   - `multi-slack-mvp.patch` (**1,372 lines** → `gateway/platforms/slack.py`,
     `gateway/session.py`, tests). This is a substantial *feature fork* of
     Hermes carried as a patch file.
2. **Runtime string-surgery monkeypatching** (`src/mac/hermes_startup.py`):
   `_apply_gateway_runtime_shim()` reads `gateway/run.py` as text and does
   exact-string `.replace()` on needles like
   `"        runtime_kwargs = _resolve_runtime_agent_kwargs()\n"` and
   `"        model = _resolve_gateway_model(user_config)\n"`. When upstream
   changes those lines, the needle silently fails to match and the function
   records `"...upstream gateway/run.py changed"`.
3. **Separate process, separate venv, separate event loop** at runtime:
   `deploy-mac-fleet.sh:4219` runs the gateway as
   `exec .../hermes-agent/.venv/bin/python .../hermes gateway run --replace`,
   while `mac` runs as its own `mac.service`.

Upstream Hermes had a commit **on the same day this ADR was written**
(`b1a25404b`, PR #35532). So mac's patches and string-surgery shims sit on
top of a target that moves daily.

### 2. The "inexplicable provider behavior" dark spot is mechanically explained

The per-agent model/provider override (the mechanism that prevents an
"agent monoculture" by putting different agents on different model families)
is injected by item (2) above — string surgery against `gateway/run.py`.
When upstream drifts and the needle misses:

- the override silently does not apply,
- the agent runs on whatever the unpatched gateway defaults to,
- and **nothing in the agent's own runtime can explain why**, because the
  decision was supposed to be made by code that was never actually inserted.

mac can only observe Hermes as *filesystem metadata* — `hermes_startup.py`
inventories `SOUL.md`, `state.db`, `gateway/run.py`, etc. by path / size /
mtime (`_file_ref`), never by live state, because it is a different process
it shares no memory with. "Observe, coordinate, keep interactive" is
structurally capped at what you can see across a process boundary plus a
text-diff of someone else's source tree.

### 3. The runtime surface is large, and Hermes is opinionated

| Hermes runtime dir | py files | py LOC |
| --- | ---: | ---: |
| `hermes_cli/` | 119 | ~111k |
| `gateway/` | 60 | ~82k |
| `tools/` | 99 | ~72k |
| `agent/` | 108 | ~68k |
| `skills/` | 43 | ~13k |

≈ **350k LOC** of runtime, versus mac's ≈ 43k. Hermes is ~8× mac. It
**exact-pins every dependency** (a deliberate supply-chain stance after the
Mini Shai-Hulud worm) and requires Python ≥ 3.11; mac uses loose ranges and
claims ≥ 3.9. "Merge Hermes into mac" is really "mac becomes the control
plane of a forked Hermes."

### 4. tokenhub is mature, sophisticated infrastructure

Per `tokenhub/README.md`: a multi-objective routing engine, **Thompson-sampling
contextual bandit** model selection, AES-256-GCM/Argon2id encrypted vault,
optional **Temporal** durable workflows with circuit-breaker fallback,
health tracking, **an SSE event bus that already broadcasts routing
decisions** (success/error/escalation/health/model-selection), and a live
admin decision feed. 111 Go files.

This is exactly the class of mature, separately-languaged, working
infrastructure that should *not* be rewritten in Python to satisfy a "single
codebase" aesthetic. That move is the ACC failure mode.

## The decision, and why this is not failure #3

ACC (714 Rust files) and openclaw were abandoned for being "too complicated."
The reflex worry is that absorbing 350k LOC of Hermes is *more* complexity, not
less. The resolution:

- **The complexity that kills these projects is boundary complexity, not line
  count.** Two venvs + two event loops + a 1,372-line out-of-tree patch +
  runtime string surgery against a daily-moving upstream is a system *no single
  human can hold in their head*. That is the actual failure mode. Merging the
  runtime into one process, one event loop, one dependency set, one repo,
  *reduces* that complexity even though it raises the line count.
- mac is **already** a fork of Hermes (the 1,372-line patch). It currently pays
  the full cost of forking (no clean upstream tracking) while getting **none**
  of the benefit (no in-process observability, no coherent single codebase).
  This is the worst quadrant. Owning a snapshot moves us to the good quadrant.
- The discipline that keeps it from becoming ACC:
  1. **Snapshot-and-prune.** Vendor only the runtime surface mac actually runs
     (`agent/`, `gateway/`, `providers/`, the `hermes_cli` runtime modules, the
     `skills`/`tools` actually invoked). **Drop** `website/` (731 files),
     `ui-tui/` (344), `web/` (97), `infographic/`, `datagen-config-examples/`,
     `locales/`, datagen/training scaffolding.
  2. **Freeze, don't chase.** "Mature" means *pinned*. We stop tracking the
     commit-of-the-day. Upstream pulls become deliberate, reviewed snapshot
     bumps — not a daily moving target under our patches.
  3. **Delete the string surgery.** The override the shim injects becomes
     first-class, owned, tested mac code (see ADR keystone below). No code path
     rewrites another file's source at runtime.
  4. **Do not absorb tokenhub.** Keep the Go service. Close the dark spot by
     *consuming its decision feed* into mac observability.

## Target architecture

```
        ┌──────────────────────────────────────────────┐
        │  mac monorepo (one Python pkg, one venv,       │
        │  one asyncio loop)                             │
        │                                                │
        │   mac control plane    in-tree vendored        │
        │   (tasks, leases,  ───▶ hermes runtime         │
        │    ledger, review)  ◀── (agent loop, gateway,  │
        │        ▲   │             providers) — IMPORTED, │
        │        │   │             not subprocessed       │
        │   agent_provider.py (owned provider decision +  │
        │        │   │            rationale, observable)  │
        └────────┼───┼───────────────────────────────────┘
                 │   │ OpenAI-compatible HTTP + decision-feed SSE
                 │   ▼
        ┌────────┴──────────┐
        │  tokenhub (Go)    │  unchanged; its SSE decision feed is
        │  routing + vault  │  consumed by mac so provider behavior
        └───────────────────┘  is legible to the agent
```

Boundaries that disappear: mac↔hermes process/venv/event-loop split; the
clone-and-patch deploy step; the runtime string-surgery shims.
Boundary that stays (but becomes transparent): mac↔tokenhub.

## Staged migration (tracked as `mac task`s, not done in one commit)

A 350k-LOC merge landed atomically is itself the ACC mistake. Stages:

1. **Keystone (this ADR's commit): own the provider decision.** `agent_provider.py`
   reproduces the shim's env-precedence resolution as pure, tested mac code that
   emits a legible rationale, and surface it in the `/startup/hermes` report so
   the *intended* provider/model is visible even when the brittle shim misses.
2. **Vendor the pruned runtime snapshot** into `src/mac/_hermes/` behind a pin
   (`deploy/hermes/SNAPSHOT.md` + `scripts/vendor-hermes-snapshot.sh`); fold the
   three patches in permanently; merge dependency manifests (adopt Hermes'
   exact-pin discipline).
3. **Import, don't subprocess.** Replace `hermes gateway run` (separate venv)
   with an in-process gateway entrypoint; gateway provider resolution calls
   `agent_provider` directly. Delete `_apply_gateway_runtime_shim` and the
   slack-activation string surgery.
4. **Single-venv deploy.** `deploy-mac-fleet.sh` stops cloning upstream and
   patching; it installs the monorepo. Retire `MAC_HERMES_AGENT_DIR`.
5. **tokenhub legibility.** mac subscribes to tokenhub's SSE decision feed and
   attributes each routing decision to the agent/turn that caused it, so
   "inexplicable provider behavior" becomes an observable, attributable event.
6. **Memory tier + observability bloat** proceed on their own tickets; they are
   not blocked by, and do not block, the merge.

## Consequences

- **Positive:** one event loop; mac can import and introspect the live agent;
  the monoculture-prevention model override is owned/tested/observable; deploy
  loses its most fragile step; the dark spot is closeable.
- **Negative / accepted cost:** mac now owns ~350k LOC of vendored runtime and
  must do deliberate snapshot bumps instead of free upstream tracking. This cost
  is *already largely paid* via the 1,372-line patch; we are making it honest.
- **Explicitly rejected:** rewriting tokenhub in Python; an atomic big-bang
  merge; continuing to clone-and-patch a daily-moving upstream.
</content>
