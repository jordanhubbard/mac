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

- [ ] **th-merge-02: OpenAI front door in mac.** `/v1/chat/completions` +
      `/v1/embeddings` accepting `model="*"`; Hermes/executor point at it via the
      same base_url contract (config flip). `MAC_ROUTER_BACKEND=inproc|tokenhub`.
- [x] **th-merge-03: multi-provider + recovering breaker.** >1 provider,
      priority/Thompson selection, a circuit breaker that **half-opens + re-probes**
      (the exact bug we hit), and fail-fast when all providers are down. Fixes
      the SPOF + the hang.
- [ ] **th-merge-04: secret vault.** Encrypted-at-rest provider keys
      (`vault://`); EMBED TokenHub's vault lib or a minimal audited
      envelope-encryption store — do NOT reinvent crypto. Gate behind review.
- [ ] **th-merge-05: native decision telemetry.** Emit `mac.router.*`
      observations in-process (reuse [[hu-05]] mapping); surface provider
      decisions in the hub UI (replaces the dropped TokenHub admin UI).
- [ ] **th-merge-06: weekly wildcard refresh wiring.** Fold [[mac-nyx7]] into the
      in-proc router's model ladder when backend=inproc.

## Guardrails (stay minimal)
Port routing + vault + the OpenAI surface only. NOT the admin UI, NOT Temporal,
NOT feature parity — that long tail stays behind the `tokenhub` backend option.
This partially supersedes ADR 0001's "keep TokenHub separate".
