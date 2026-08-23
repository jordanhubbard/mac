# ADR 0029: The coding-route search path is a fleet contract, not per-worker environment

- Status: Proposed
- Date: 2026-08-22
- Decision owner: MAC fleet owner
- Related:
  - [ADR 0003](0003-tokenhub-core-into-mac.md) — the in-mac router and its
    recovering circuit breaker, whose semantics this reuses
  - [ADR 0013](0013-authoritative-hub-allocator.md) — the hub is the authority;
    worker state is a cache
  - [ADR 0017](0017-token-spend-is-metered-at-the-router.md) — spend is metered
    where routing happens
  - [ADR 0026](0026-first-class-operations-emit-bus-events.md) — a first-class
    operation emits a bus event

## Context

### What actually happened

Through development the fleet owner repeatedly moved work between Codex, Claude,
Cursor, OpenCode and Pi as monthly account credits ran out. Each move was a
change to *environment*: a pinned `MAC_CODING_AGENT`, an exported key, an edited
unit file, a re-provisioned worker. The fleet's real route search path therefore
lived nowhere. It was re-derived on every host from whatever happened to be set
there, and no two hosts were obliged to agree.

Two costs follow directly, and both were observed:

1. **The same cap is paid for repeatedly.** When one account exhausts, every
   worker discovers it independently, one wasted turn at a time. Nothing carries
   "this credential is capped until the month rolls" from the worker that
   learned it to the twenty that are about to learn it.
2. **Recovery is invisible.** Quotas replenish asynchronously, and an
   organisation can raise a limit without telling anyone. A fleet that responded
   to exhaustion by editing environment does not go back — the edit is
   permanent, so the cheapest route stays abandoned until a human remembers it.

On 2026-08-19 the failure mode reached its limit: every node reported all three
of the then-configured CLIs unavailable at once and the fleet completed one task
in twenty-four hours.

### What exists today

- `mac.coding_agent.AGENT_PRIORITY` is a hard-coded tuple, `("opencode", "pi",
  "claude", "codex", "cursor")`, with a long and honest comment explaining that
  the ordering is about *credential durability*, not cost. It is the right
  ordering for the reason given and it is still a constant in a source file:
  the owner cannot change it without a code change, and it is not the owner's
  ordering.
- `mac.provider_router.ProviderRouter` has exactly the recovering breaker this
  problem needs — CLOSED → OPEN → cooldown → HALF_OPEN probe → CLOSED — built
  after the retired standalone service tripped a breaker that never half-opened
  and silenced the fleet. But it ranks *API providers* only, it is
  process-local, it has no notion of a coding-CLI or of which *account* backs a
  route, and nothing it learns leaves the process.
- `CodingAgentChoice.route_fingerprint()` already produces a secret-free
  identity for a resolved route. Nothing consumes it as a fleet-wide key.

So the parts exist and are not connected, and the missing connection is exactly
the expensive one: no shared identity, no shared order, no shared learning.

### Constraints that shape the decision

- Coding-CLI subscriptions have the best cost/token ratio available, so they
  must be exhausted in owner-defined order before any direct inference API.
- OpenCode and Pi expose a pluggable provider+model matrix, so a route is not
  identified by its CLI alone.
- Price cannot be inferred from a model name and must not be guessed.
- A worker can only use a route whose CLI and credential it locally holds.
- Nothing published may contain a token, an account identifier, a balance, raw
  provider output, or an authenticated URL.

## Decision

### 1. A route has an identity, and the identity is five names

One route key is **harness/CLI type + credential SOURCE NAME + provider + model
+ endpoint class**. Never a token, never an authenticated URL. `RouteKey` in
`mac.route_ladder` enforces this at construction rather than at publication:
an identity that embeds a credential must not be constructible, because the
fingerprint is precisely the value every consumer copies around.

The pair *(harness, credential source)* is `credential_identity` — the thing a
monthly cap actually belongs to. A cap belongs to the account, not to whichever
model was requested when it was hit; suppressing only the exact model would let
the next worker re-prove the same cap with a different one.

### 2. The order is the owner's, and it is a document

`mac.coding_route_ladder.v1` is an ordered list of route identities with a rank.
Rank 0 is cheapest/most preferred; larger is progressively more expensive.
Within an equal rank, harness/CLI type is the major key and model is only a
tie-break — a deterministic one, so two workers reading one document choose the
same route, and explicitly **not** a price signal.

Subscription-backed CLIs precede direct-API routes by default. Owner
configuration is authoritative and overrides that default without argument.

An unconfigured fleet keeps `AGENT_PRIORITY`. A ladder that is present but
unparseable is reported and the built-in order is used: a fleet running on a
route order nobody wrote is the failure worth being loud about.

### 3. Failure class decides whether the ladder moves at all

Seven classes, closed set: `quota_exhausted`, `rate_limited`, `auth`,
`provider_outage`, `model_unavailable`, `transport`, `semantic`.

Only the first six say anything about the *route*. `semantic` means the model
answered and the work was wrong — the route is fine, and demoting it would cost
the fleet its cheapest option on the strength of a failing test.

Classification defaults to `semantic`, not to "unavailable". An unrecognised
error string is not evidence that a route is gone, and guessing the other way
demotes rank 0 for free. Body text outranks HTTP status, because providers
routinely return a monthly cap as a 429: the status says "throttled", the body
says "you are out of credits", and only the body is true.

### 4. Suppression reuses the existing recovering breaker

`RouteLadder` composes `ProviderRouter` rather than adding a second, disconnected
health policy. The breaker already has the property this needs and the retired
service lacked — a half-open probe that lets a route come back.

The ladder adds one thing the breaker could not know: a **per-failure-class
dwell time**. An account cap is proven by one response and should not be
re-proven for an hour; a 429 should be re-probed in a minute; transport noise
should still need a run of failures before it costs a route its place. That is
one state machine with a class-aware cooldown, not two policies.

Recovery is a *bounded* half-open probe: exactly one worker gets the probe and
the rest keep falling back, so a refreshed monthly quota is rediscovered without
every worker paying to rediscover it simultaneously.

### 5. Outcomes are published, and only account facts travel

Agents publish secret-free `mac.route_availability.v1` records over AgentBus and
into durable fleet learning: route fingerprint, rank, reporting agent, outcome,
failure class, observed and cooldown timestamps, and *bounded, scrubbed*
evidence.

A received record is validated to the same standard as a locally produced one,
including reconstructing the `RouteKey`, which re-runs the secret-free check on
every field before anything is stored or re-published.

`quota_exhausted` and `auth` are facts about an account and suppress that exact
credential-backed route fleet-wide. `rate_limited`, `provider_outage` and
`transport` are facts about the reporting *host* and stay there — otherwise one
sick worker suppresses the fleet's cheapest route for everyone.

A success supersedes an older failure, including a fleet-wide cap, because a
refreshed quota or a raised organisation limit is exactly the event nobody is
told about in advance.

### 6. Switching happens at a turn boundary, never mid-turn

`begin_turn()` pins the route for the turn. A cheaper route that becomes
eligible mid-turn is reported by `pending_switch()` and taken at the next
`begin_turn()`. An in-flight executor keeps the route it started on: swapping
harness under a running turn throws the turn away, which costs more than
finishing one turn on a more expensive route.

### 7. Fleet learning removes options; it never grants one

The hub's durable outcomes are authoritative and local worker state is a cache;
AgentBus accelerates convergence and is not the authority. Independently of
both, `RouteCapability` gates selection on what this host can actually run. A
peer proving a route works does not install that CLI or hand over its
credential, and no amount of fleet consensus may select a route a worker cannot
execute.

### 8. Relay cost is advisory evidence, and unknown is not zero

NeMo Relay supplies measured token/cost/latency where available. It is advisory:
visible in telemetry, usable for tie-breaking *within* owner-authorised policy,
and never able to silently reorder the owner's ranks.

Unmeasured cost is `unknown` — explicitly not `0`. A subscription route with no
Relay measurement would otherwise sort as free and win every tie-break it should
have lost.

Relay observes; it does not route. Provider selection stays with the in-MAC
ladder and router, and MAC's SecretsService keeps owning credentials. Nothing
here re-admits the retired standalone TokenHub as a backend, fallback, or
architectural reference.

### 9. One ladder, one selection point

`mac.coding_agent.resolve_coding_agent` is the single selection point every
worker, executor and reviewer already goes through, so *it* reads the ladder. A
second ordering living beside it would be the per-worker accident again with a
schema on top.

## Consequences

- The owner can re-rank the fleet's routes by editing one document, and every
  worker, executor and reviewer picks it up. That is the point.
- A quota cap costs the fleet one wasted turn instead of one per worker, and
  the cap is visible as a named suppression with a countdown rather than as a
  run of unexplained failures.
- A refreshed quota is rediscovered automatically, by one probe rather than a
  stampede, and the fleet returns to the cheaper route at the next boundary.
- New state exists that can be wrong: a mis-ranked document routes the whole
  fleet expensively, and a bad `quota_exhausted` classification suppresses a
  working route for the full cooldown. Both are visible in telemetry and both
  clear on the next success, which is the trade against silence.
- Selection can now return "nothing" when every rung is suppressed. Callers
  must fail closed on it. That is deliberate: hanging on a route the fleet has
  already proved is capped is the failure this replaces.
- `ProviderRouter.record_failure` grows two keyword arguments. Existing callers
  are unaffected; the breaker's behaviour without them is unchanged.

## Alternatives considered

**Leave `AGENT_PRIORITY` as a constant and keep tuning it.** Rejected. The
ordering it encodes is defensible and it is still not the owner's, cannot vary
per fleet, and cannot respond to a cap at all.

**Extend `ProviderRouter` to cover coding CLIs directly.** Rejected as the whole
answer, adopted as the mechanism. The breaker is process-local and has no
account identity; teaching it those would make it two things. Composing it keeps
one health state machine and adds the ladder above it.

**Let Relay decide the route from measured cost.** Rejected. Relay observes
tool/model/token/cost activity; it is not the inference router unless a separate
accepted contract says so. Cost that silently reorders the owner's ladder is
cost that silently overrides the owner.

**Have the hub hand every worker its route.** Not rejected, deferred. The hub is
already the authority for durable outcomes, and a central assignment would also
work. It needs the worker to be able to say "I cannot run that" and fall back
locally, which is the capability check above — so this decision is a
prerequisite either way, and the shared identity and order it defines are what a
future central assignment would assign.

**Broadcast every failure class fleet-wide.** Rejected. A worker with a broken
network would suppress rank 0 for everyone. Only account-level facts generalise
across hosts.
