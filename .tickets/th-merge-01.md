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
- [x] **th-merge-06: wildcard ladder IN the router** (PR #27). `"*"` resolves to
      rank-0 and substitutes the next model on a 404/422 (on-the-fly LLM
      substitution); provider-level failures still drive the recovering breaker.
      The router no longer needs TokenHub to resolve the wildcard.
- [~] **th-merge-07: direct cutover + retire standalone TokenHub.** Escrow tool
      (PR #28) + env-file shell-quoting fix + direct cutover (PR #29).
      `natasha` is LIVE direct-to-upstream — TokenHub out of its path.
      Remaining: escrow the broader vault keys (TOKENHUB_VAULT_PASSWORD group)
      for full model breadth; cut over rocky + bullwinkle; stop the standalone
      TokenHub service.

## Status (2026-06-01)
**Direct cutover is LIVE on `natasha`.** Its gateway → its local in-mac router →
`inference-api.nvidia.com` **directly**, key resolved from the encrypted vault
(`secret:nvidia-upstream`). TokenHub is entirely out of natasha's path. Verified
live: non-stream 200 (`us/azure/openai/gpt-4.1-mini`) + streaming SSE. rocky +
bullwinkle still on TokenHub (untouched).

The in-mac router is now a **complete TokenHub replacement** in code: OpenAI
front door, streaming passthrough, recovering breaker + fail-fast, multi-provider
failover, wildcard ladder, and escrowed-key resolution from the existing
`SecretsService`. The escrowed key is scoped to litellm's `default-models` group
(gpt-4.1-mini class — what natasha's `"*"` already resolved to); the broader
ladder (meta/llama-*, etc.) needs the vault-held keys escrowed too.

Remaining to retire TokenHub fleet-wide: escrow the broader keys, cut over rocky
+ bullwinkle (per-agent like natasha, or centralized hub `/v1` on the tailnet),
then stop `mac-tokenhub`.

## Guardrails (stay minimal)
Port routing + vault + the OpenAI surface only. NOT the admin UI, NOT Temporal,
NOT feature parity — that long tail stays behind the `tokenhub` backend option.
This partially supersedes ADR 0001's "keep TokenHub separate".
