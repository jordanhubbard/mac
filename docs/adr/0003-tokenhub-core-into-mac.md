# ADR 0003 — Optional in-mac model router + vault (revisiting TokenHub's boundary)

- Status: **Proposed** (revisits the TokenHub decision in [[ADR 0001|0001-unify-hermes-runtime-into-mac]])
- Date: 2026-05-31
- Decision owner: Jordan Hubbard
- Context: ADR 0001 chose to **keep TokenHub separate** ("mature Go infra;
  the dark spot is observability, not a boundary problem"). Two things have
  changed that judgment:
  1. **Operational reality.** This session TokenHub wedged the entire fleet:
     a single configured provider (`nvidia`), a circuit breaker that tripped
     and never half-opened to recover, and no fast-fail — so completions hung
     and every agent went silent. A separate service that can take down the
     whole hub is exactly the failure-domain cost ADR 0001 under-weighted.
  2. **Leverage reassessment (owner).** TokenHub's *actual* leverage is two
     things — **provider routing** (`model="*"` → pick a provider, with
     failover) and the **secret vault**. The rest (notably the admin **UI**) is
     "probably most of the code, and is not necessarily used — or could be
     re-imagined in the hub UI, which is still evolving."

So the boundary question flips: if the UI is most of the code and isn't load-
bearing, the *core* (routing + vault) is small enough to live in `mac` as an
option — the same in-process win the Hermes merge delivered.

## TL;DR

Build an **optional, in-`mac` model router + vault** that subsumes TokenHub's
core leverage behind the OpenAI-compatible surface `mac`/Hermes already speak
(`/v1/chat/completions`, `/v1/embeddings`, `model="*"`). Standalone TokenHub
becomes **one of two interchangeable backends**, selected by config. **Drop the
admin UI** (re-imagine routing visibility in the evolving hub UI, fed by the
[[hu-05]] decision feed → native observations). Keep it **minimal and optional**
— this is explicitly *not* a feature-parity rewrite (that is the ACC/openclaw
"too complicated" trap ADR 0001 warned about).

## Decision

Provide `mac.router` (optional, in-process on the hub):

1. **OpenAI-compatible front door** — `/v1/chat/completions` + `/v1/embeddings`
   served by mac, accepting `model="*"`. Hermes/the executor point at it
   unchanged (same base_url contract), so adopting it is a config flip.
2. **Multi-provider config + health-aware routing** — fixes the live SPOF:
   more than one provider, priority + Thompson/round-robin selection, and a
   **circuit breaker that half-opens and re-probes** (the exact bug we hit:
   the breaker tripped on a transient and never recovered even though the
   upstream was healthy). Plus **fail-fast** when all providers are down
   (return "no provider available" immediately — never hang).
3. **Secret vault** — an encrypted at-rest store for provider keys, addressed
   the same way (`vault://...`). To avoid a risky crypto rewrite, prefer
   **embedding TokenHub's existing vault library** or a minimal audited
   envelope-encryption store; the vault is the one genuinely security-sensitive
   piece and gets the most scrutiny.
4. **Native decision telemetry** — emit `mac.router.*` observations directly
   (no SSE bridge needed when in-process); reuse the [[hu-05]] mapping so the
   hub UI can show per-turn provider decisions.
5. **TokenHub stays an option** — `MAC_ROUTER_BACKEND = inproc | tokenhub`.
   Existing deployments keep the Go service; new/simple ones use the in-mac
   router and shed a service + a failure domain.

## Explicitly out of scope (the "stay minimal" guardrails)

- The TokenHub **admin UI** — dropped; visibility moves to the hub UI.
- **Temporal workflows** — only if a concrete need appears; not ported wholesale.
- **Feature-parity** with every TokenHub knob. Port the routing policy + vault +
  the OpenAI surface; leave the long tail behind the `tokenhub` backend option
  for anyone who needs it.

## Consequences

- When `inproc` is selected, the hub has **one fewer service that can wedge it**
  (the session's recurring lesson), and provider config/secrets are managed in
  the same place as everything else.
- The live misconfig is fixed *by construction*: multi-provider + a recovering
  breaker + fail-fast are requirements of the in-mac router, not afterthoughts.
- This **partially supersedes ADR 0001's "keep TokenHub separate"** — narrowed
  to: keep it separate *as an option*, but its core is no longer off-limits to
  merge.

## Risks

- **Vault security** — reimplementing crypto is the trap; embed/reuse, don't
  reinvent. Gate behind review.
- **Scope creep** — the failure mode is rebuilding all of TokenHub. The
  `inproc` router must stay small (routing + vault + OpenAI surface) or it
  becomes the thing ADR 0001 warned against.

Tracked in **th-merge-01**.
