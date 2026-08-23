# The coding-route ladder

The ladder is the fleet's route search path: an ordered list of coding routes
the owner writes down once, which every worker, executor and reviewer reads.
It replaces the older arrangement where the order was a constant in
`mac/coding_agent.py` and the actual route was whatever each host's environment
happened to select.

The reasoning is in [ADR 0029](adr/0029-the-route-search-path-is-a-fleet-contract.md).
This page is the schema and the operator view.

## Configuring it

Point one of two variables at a `mac.coding_route_ladder.v1` document:

| Variable | Meaning |
| --- | --- |
| `MAC_CODING_ROUTE_LADDER_FILE` | Path to the document. Wins if both are set. |
| `MAC_CODING_ROUTE_LADDER` | Inline JSON (starts with `{`), or a path. |

Unset means no ladder is configured, and selection keeps `coding_agent`'s
built-in order. A document that is set but unparseable is **reported in the
selection rationale** and the built-in order is used — the fleet keeps working
and says why, rather than silently running on an order nobody wrote.

## `mac.coding_route_ladder.v1`

```json
{
  "schema": "mac.coding_route_ladder.v1",
  "policy": {
    "quota_cooldown_seconds": 3600,
    "rate_limit_cooldown_seconds": 60,
    "auth_cooldown_seconds": 900,
    "outage_cooldown_seconds": 300,
    "model_unavailable_cooldown_seconds": 1800,
    "transport_cooldown_seconds": 120,
    "failure_threshold": 3,
    "half_open_max_probes": 1
  },
  "routes": [
    {
      "rank": 0,
      "harness": "codex",
      "credential_source": "codex-oauth-file",
      "provider": "openai",
      "model": "gpt-5.5",
      "endpoint_class": "subscription"
    },
    {
      "rank": 1,
      "harness": "claude",
      "credential_source": "claude-oauth-file",
      "provider": "anthropic",
      "model": "claude-opus-5",
      "endpoint_class": "subscription"
    },
    {
      "rank": 2,
      "harness": "opencode",
      "credential_source": "OPENCODE_API_KEY",
      "provider": "openrouter",
      "model": "qwen3-coder",
      "endpoint_class": "direct_api"
    }
  ]
}
```

### Route fields

| Field | Required | Meaning |
| --- | --- | --- |
| `rank` | no | 0 is cheapest / most preferred. Omit it and the written position is used. |
| `harness` | yes | CLI type: `codex`, `claude`, `cursor`, `opencode`, `pi`. |
| `credential_source` | yes | The **name** of the credential source — an env var name or a config path. Never the value. |
| `provider` | yes | Inference provider the route reaches. |
| `model` | no | Model id. Empty means "whatever the harness resolves". |
| `endpoint_class` | no | `subscription`, `direct_api`, or `self_hosted`. Defaults to `subscription`. |
| `enabled` | no | `false` parks a rung without deleting it. |
| `note` | no | Free text for the owner; scrubbed and bounded like any published string. |

Two routes may not share all five identity fields — one identity cannot hold two
ranks, and a document that tries is rejected rather than half-applied.

### Ordering rules

- The owner's `rank` is authoritative.
- Within an equal rank, `harness` is the major key and `model` is only the final
  tie-break. It is a deterministic tie-break so that two workers reading the
  same document choose the same route. **It is not a price signal** — nothing
  infers cost from a model name.
- Subscription-backed CLIs before direct-API routes is the *default* intent, not
  a rule the code enforces. Rank as you mean it.

### Never put in a route

Tokens, keys, account identifiers, balances, authenticated URLs, or URLs with a
query string. These are rejected at construction, not at publication — the route
fingerprint is what every consumer copies around, so an unsafe identity must not
be constructible in the first place.

## Failure classes

Exactly one class per failure. Only the first six affect the ladder.

| Class | Affects the ladder | Travels to peers | Default dwell |
| --- | --- | --- | --- |
| `quota_exhausted` | yes, on one observation | yes — an account fact | 1 h |
| `auth` | yes, on one observation | yes — an account fact | 15 min |
| `rate_limited` | yes, on one observation | no — host-local | 1 min |
| `model_unavailable` | yes, on one observation | no | 30 min |
| `provider_outage` | after `failure_threshold` | no | 5 min |
| `transport` | after `failure_threshold` | no | 2 min |
| `semantic` | **no** | — | — |

`semantic` means the model answered and the work was wrong. The route is fine.
Demoting it would cost the fleet its cheapest option because a test failed.

Classification defaults to `semantic` when nothing matches: an unrecognised
error is not evidence a route is gone. Body text beats HTTP status, because a
monthly cap is routinely delivered as a 429 — the status says "throttled", the
body says "out of credits", and only the body is true.

## `mac.route_availability.v1`

What an agent publishes after a route succeeds or fails.

```json
{
  "schema": "mac.route_availability.v1",
  "route": {
    "harness": "codex",
    "credential_source": "codex-oauth-file",
    "provider": "openai",
    "model": "gpt-5.5",
    "endpoint_class": "subscription",
    "fingerprint": "sha256:..."
  },
  "rank": 0,
  "agent_id": "worker-1",
  "outcome": "failure",
  "failure_class": "quota_exhausted",
  "affects_availability": true,
  "observed_at": 1755907200.0,
  "cooldown_until": 1755910800.0,
  "evidence": "monthly usage limit reached"
}
```

`evidence` is scrubbed and bounded (240 characters). Raw provider output is
never republished.

A received record is validated exactly as a locally produced one, including
rebuilding the route identity — so the secret-free check runs again on every
field before anything is stored or forwarded.

### What a peer's record does

- **Failure, `quota_exhausted` or `auth`**: suppresses that exact
  credential-backed route locally, for the configured dwell. A cap on one
  account never suppresses the same CLI on another.
- **Failure, anything else**: ignored. A peer's broken network is not yours.
- **Success**: clears suppression, including a fleet-wide cap — a refreshed
  quota or a raised organisation limit is exactly what nobody is told about in
  advance.
- **Success on a route this host cannot run**: still clears suppression, still
  selects nothing. Fleet learning removes options; it never grants one.

## Recovery, and switching back

When a cooldown elapses the route goes half-open and **one** worker gets a
probe; everyone else keeps falling back. If the probe succeeds the route closes
and is cheapest again; if it fails the cooldown restarts. That bound is what
stops a refreshed monthly quota from being rediscovered by the whole fleet at
once, each paying for the same discovery.

Switching happens at a **turn boundary**. A cheaper route that becomes eligible
mid-turn shows up as `pending_switch` and is taken at the next turn. An
in-flight executor keeps the route it started on, because swapping harness under
a running turn throws the turn away.

## Cost, and why unknown is not zero

NeMo Relay supplies measured token/cost/latency where it is available. It is
**advisory**: surfaced in telemetry, usable for tie-breaking within
owner-authorised policy, and never able to reorder the owner's ranks.

A route with no measurement reports `{"known": false, "usd_per_million_tokens":
null}`. It does not report `0`. A subscription route with no Relay measurement
would otherwise sort as free and win every tie-break it should have lost.

## Telemetry

`RouteLadder.telemetry()` emits `mac.route_ladder.telemetry.v1`, and per rung
answers the five questions an operator actually asks: what rank the owner gave
it, whether it is the effective route, why it is suppressed and for how long
(`seconds_until_probe`), what Relay measured it to cost or that nobody measured,
and when it last actually worked.

The document is secret-free by construction and JSON-serialisable as published.
