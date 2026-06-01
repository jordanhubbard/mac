---
id: th-merge-01
status: open
deps: []
links: [mac-nyx7, hu-05, mem-store-02]
created: 2026-05-31T00:00:00Z
type: feature
priority: 1
audit: tokenhub-core-into-mac
discovered_via: architecture_review
---
# EPIC: optional in-mac model router + vault (ADR 0003)

Spec: `docs/adr/0003-tokenhub-core-into-mac.md`. Subsume TokenHub's core
leverage (provider routing + secret vault) into mac as an OPTION behind the
OpenAI-compatible surface; drop the UI; keep standalone TokenHub as a backend.
Motivated by the live wedge (single provider + stuck breaker + no fail-fast).

## Work breakdown

- [x] **th-merge-02: OpenAI front door in mac.** `/v1/chat/completions` +
      `/v1/embeddings`; `MAC_ROUTER_BACKEND=inproc|tokenhub` (PR #20).
- [x] **th-merge-02b: streaming + wildcard resolution + auth** (PRs #22/#23/#24).
      The minimal proxy could not replace TokenHub: (a) the gateway streams via
      `responses.stream()` — added `stream_complete` + `urllib_stream_forwarder`
      (failover/breaker on the status line, then SSE passthrough); (b) the real
      upstream rejects `model="*"` — added `default_model` `"*"`→concrete
      resolution; (c) `/v1` now requires the `agent` scope and the gateway
      presents a mac token (so it is never an open proxy + caller-auth is
      separate from upstream-auth). Deploy plumbs `MAC_ROUTER_*` (fleet-scoped)
      + per-agent local-router cutover.
- [x] **th-merge-03: multi-provider + recovering breaker** (PR #19). Half-open
      re-probe + fail-fast. Fixes the SPOF + the hang.
- [x] **th-merge-04: secret vault.** NOT reinvented — mac already ships the
      Python encrypted escrow (`SecretsService`: Fernet at-rest, scoped access,
      single-use reveal, rotation, audit). PR #25 wires the router to it:
      provider `key=secret:<name>` → `SecretsService.resolve_secret_value`
      (audited, decrypt-at-use). Upstream key is escrowed, never plaintext env.
- [ ] **th-merge-05: native decision telemetry.** Emit `mac.router.*`
      observations in-process (reuse [[hu-05]] mapping); surface provider
      decisions in the hub UI (replaces the dropped TokenHub admin UI).
- [ ] **th-merge-06: wildcard ladder IN the router.** Fold [[mac-nyx7]]'s ladder
      into the in-proc router: resolve `"*"` to rank-0, and on a model-level
      failure substitute rank-1.. (on-the-fly LLM substitution) so the router
      no longer leans on TokenHub to resolve the wildcard.
- [ ] **th-merge-07: networked-hub rollout + retire standalone TokenHub.** Bind
      the hub's mac `/v1` on the tailnet; store the upstream key as
      `secret:nvidia-upstream`; point spokes at the hub router (they present
      their agent token); drop the per-spoke TokenHub dependency. End-state:
      centralized hub router+escrow, TokenHub gone as a failure domain.

## Status (2026-05-31)
A **wrap-TokenHub canary** is LIVE on `natasha`: gateway cut over to its local
in-mac router (recovering breaker + streaming passthrough), forwarding through
TokenHub with natasha's own key. Streaming chat verified end-to-end; rest of the
fleet untouched. Routing code is proven on live traffic. Remaining to fully
retire TokenHub: th-merge-06 (ladder) + th-merge-07 (networked rollout); the
direct cutover now needs only an operator to escrow the upstream key.

## Guardrails (stay minimal)
Port routing + vault + the OpenAI surface only. NOT the admin UI, NOT Temporal,
NOT feature parity — that long tail stays behind the `tokenhub` backend option.
This partially supersedes ADR 0001's "keep TokenHub separate".
