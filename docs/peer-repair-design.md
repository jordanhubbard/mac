# Peer Repair: agents repairing agents

- Status: **Proposed** (not accepted; no code written). The credential
  survivability track (§7.2, §7.3, §8b) is carried on `docs/roadmap.md` under
  *Operational autonomy*.
- Date: 2026-09-22, revised 2026-09-23
- Scope: agent-to-agent repair (§1–§6), plus two observability tracks the
  investigation forced out (§7). Hub *failover* is surveyed and deliberately
  **not** proposed — it conflicts with an existing accepted decision, and that
  conflict is the finding, not an oversight.
- Related: `docs/hub-availability.md`, `docs/roadmap.md`, ADR 0013
  (authoritative hub allocator), ADR 0014 (visibility is not a dispatch gate)

## 1. Why this document exists

On 2026-09-22 a fleet-wide deploy to `ovswarm` died mid-flight. It completed
Phase 1 prepare on the hub node at 13:28:26Z, then the deploy process
vanished — no crash log, no later phase artifacts, no process on the host.
At 14:00:04Z the fail-forward monitor gave up and wrote
`fail-forward-20260922T131604Z.json` (`status: retained_for_forward_repair`,
`rollback_performed: false`), and 49 of 53 `ovswarm` agents were left holding
`dispatch_hold` with reason `"mac admin fleet roll-forward repair retained
after 20260922T131604Z"`.

Three hours later the fleet was still stuck, and it was found by a human
running `mac admin fleet creds-status` for an unrelated reason.

This is not a new failure mode. `self_healing.py:593-596` records the previous
occurrence in its own docstring:

> A failed deploy leaves an agent held for "roll-forward repair" with no
> companion process to ever notice or release it — observed live on
> 2026-09-01: three agents stayed held for hours across two failed deploy
> attempts until an operator went looking.

The premise of this design is that **noticing a sick peer should be a group
responsibility of the fleet, not a privilege of whoever runs a CLI command.**

## 2. What already exists (verified 2026-09-22 against `origin/main` @ eaf896c7)

Most of the pipeline is already built. An implementer should extend it, not
rebuild it.

| Capability | Where | State |
| --- | --- | --- |
| Detect a silent/degraded peer | `self_healing.py:791` `_check_agent_unhealthy` | **exists** |
| Detect an abandoned deploy hold | `self_healing.py:592` `_check_stale_deploy_hold` | **exists** |
| File a repair task from a finding | `self_healing.py:952` `_file_fix_task` → `control_plane.create_task` | **exists** |
| Re-file with attempt count + rollback context on recurrence | `self_healing.py:964-971` | **exists** |
| Per-cycle spawn budget + fingerprint dedup | `self_healing.py:82` (`DEFAULT_MAX_TASKS_PER_CYCLE = 10`), `_act_on_findings:845` | **exists** |
| Single-winner atomic claim | `services.py:12551` `claim_task_v2` (optimistic CAS row-lock) | **exists** |
| Lease expiry → task returns to the pool | `services.py:14488` `_claim_lease_expiry_finalization` | **exists** |
| Capability matching enforced at dispatch | `allocator.py:957-960`; unsatisfiable check `diagnostics.py:285` | **exists** |
| Hard pin of a task to one agent | `allocator.py:955` (`target_agent_id`) | **exists** |
| Agent-to-agent messaging | `agentbus_schemas.py:64` `mac.agent.peer_message.v1`, `:75` `peer_reply.v1`, direct addressing via `to_agent_id`/`to_agent_ids` | **exists** |
| Task lifecycle broadcast on the bus | `agentbus_schemas.py:153` `mac.task.lifecycle.v1` | **exists** |
| Election / leader / failover / buddy vocabulary | — | **absent everywhere** |
| Locality (proximity) on an agent record | — | **absent everywhere** |

**The timeout-and-rehand-off behaviour asked for in the premise already
exists** as lease expiry: a claimed task whose 900s lease lapses is finalized
exactly-once and returns to the open pool for another agent. No new timeout
machinery is needed for that half.

## 3. Measured evidence that the existing loop does not repair anything

Counted on the `rocky` hub, 2026-09-22, across all projects and all states:

| Self-heal tasks | Count |
| --- | --- |
| Total filed | **89** |
| Completed | **1** |
| Failed | 11 |
| Cancelled | 77 |
| Non-terminal (still actionable) | 0 |

Of those, **30 were `agent_unhealthy` findings — a peer detected as silent or
degraded — and every single one is `cancelled`. None completed.**

So the fleet has been detecting dead peers and filing repair tasks for months
at roughly a 1% success rate. Detection is not the bottleneck. **Assignment
and execution are.**

## 4. Four defects that explain the 1%

Each is independently verifiable and independently fixable.

### D1 — Repair tasks declare no capabilities, so nobody competent is preferred

`_file_fix_task` (`self_healing.py:993-998`) calls `create_task(title,
description=..., metadata=..., actor=...)`. It never sets
`required_capabilities`. Confirmed on a live task
(`task_4b2c41b9cf7a4a3b8366ba491c56a8c4`, "Self-heal: agent
agent_jordanh-worker1 is heartbeating but degraded/offline"):
`required_capabilities: None`.

Consequence: a task whose content is "SSH to another host and restart its
supervisor" is claimable by any idle agent in the fleet, including agents with
no route to the sick host and no privilege to restart anything. The allocator's
capability machinery (`allocator.py:957`) is fully functional — it is simply
never given a requirement to enforce.

### D2 — `stale_deploy_hold` pins its repair task to the held agent, making it undispatchable

`_check_stale_deploy_hold` sets `target_agent_id=agent_id`
(`self_healing.py:633`) where `agent_id` is, by the definition of the finding,
an agent under `dispatch_hold`. But:

- `allocator.py:955` — `target_agent_id` is a **hard pin**: every other agent
  is rejected `AGENT_TARGET_MISMATCH:pinned`.
- `allocator.py:937` — a held agent is itself rejected `AGENT_HELD`.

The pinned agent is the only one allowed to claim, and it is the one agent
forbidden from claiming. **The task can never be dispatched to anyone.**

The sibling check knows this hazard and avoids it. `_check_stuck_quarantine`
(`self_healing.py:585`) carries the comment:

> `# Deliberately unpinned: the held agent cannot claim work.`

The same reasoning was not applied to `_check_stale_deploy_hold`.

This is currently **latent, not observed**: the only fleet that accumulated
roll-forward holds (`ovswarm`) has self-healing disabled (see D3), so the
deadlocked task was never filed. Enabling self-healing on `ovswarm` without
fixing this would produce 49 permanently undispatchable tasks.

### D3 — Self-healing is disabled on the fleet that needed it

`self_healing.py:138` gates the entire sentinel on `MAC_SELF_HEAL_ENABLED`.

- `rocky` hub `~/.mac/mac.env`: `MAC_SELF_HEAL_ENABLED=1`
- `ovswarm` hub (`10.57.228.137`) `~/.mac/mac.env`: **not set**

Verified consequence: `mac --fleet ovswarm-hub task list --all --all-states`
returns **zero** tasks whose title begins `Self-heal:`. The fleet that sat
broken for three hours had no detector running at all.

This is a deployment-defaults defect, not a code defect, but it is the reason
the 2026-09-22 incident had no automated response whatsoever.

### D4 — A failed repair leaves no machine-readable reason, so the retry is blind

`_file_fix_task` re-files on recurrence with an incremented
`self_heal_attempt` and a prose `rollback_clause` telling the next agent "do
not repeat that approach" (`self_healing.py:964-971`). That is the right
instinct, but the only signal is English prose in the description.

Nothing records *why* the previous attempt failed in a form the allocator can
act on. An agent that failed because it has no network route to the sick host
produces the same record as one that failed because the fix was wrong — so the
next claim is as likely to be the same agent hitting the same wall. There is no
mechanism to say "this class of agent cannot satisfy this task; exclude it".

The allocator already supports exclusion (`allocator.py:953`,
`task.excluded_agent_ids` / `retry_excluded_agent_ids`). It is simply never
populated by the self-healing path.

## 5. Design: preferred-cohort repair (no voting protocol)

### 5.1 Why not an election

The stated premise asks for agents to "discuss over agentbus and hold a quick
election to pick the agent which fixes the other agent." This design
deliberately does **not** implement a vote, for one reason:

**`claim_task_v2` is already an election.** It is an atomic optimistic-CAS
row-lock that yields exactly one winner under contention, backed by the
transactional authority. A vote conducted over `agentbus` would be a second,
weaker consensus mechanism layered on top of a strong one — and the two can
disagree, because the bus is a durable log with per-consumer cursors
(`agentbus_service.py:555`), not a consensus protocol. Every failure that a
vote introduces (lost ballots, quorum stalls, two agents believing they won,
the tie-break itself needing a tie-break) is a failure the CAS claim does not
have.

What the current system genuinely lacks is not *arbitration* but **candidate
quality**: it picks the first agent to grab the task rather than the best one.
That is a ranking problem, and ranking does not require consensus.

So: keep the CAS as the election. Change *who is standing for it*.

### 5.2 D1 fix — a `repair` capability

Add `repair` to the fleet capability vocabulary and set it on agents
provisioned to fix peers (has SSH reach to peer hosts, credentials, and
supervisor privileges).

`_file_fix_task` gains a `required_capabilities` argument. Findings whose
remediation is host surgery (`agent_unhealthy`, `stale_deploy_hold`,
`stuck_draining`, `stuck_quarantine`) declare `{"repair"}`. Findings that are
ledger hygiene (`nap_liveness`, stale-project sweeps) keep no requirement.

This alone converts "any idle agent" into "an agent that can actually do it",
using enforcement that already works.

**Mandatory companion check:** `diagnostics.py:285`
(`unsatisfiable-requirements`) already flags tasks no agent can satisfy. If a
fleet has zero `repair`-capable agents, every repair task silently becomes
unsatisfiable — strictly worse than today. The rollout in §8 therefore gates on
that check being clean, and `Finding` emission must fall back to no-requirement
if the fleet advertises no `repair` capability at all.

### 5.3 D2 fix — never pin a repair task to its subject

In `_check_stale_deploy_hold`, drop `target_agent_id=agent_id`
(`self_healing.py:633`) and carry the subject as
`metadata["repair_subject_agent_id"]` instead — descriptive, not dispatch-
affecting.

Then add an invariant, enforced in `_file_fix_task` and covered by a test:

> A finding whose subject is an agent must never pin its repair task to that
> agent. The subject travels as `repair_subject_agent_id`; `target_agent_id`
> is reserved for tasks that must run *on* a specific healthy host.

Additionally, the subject must be added to `excluded_agent_ids` — a sick agent
should not be the one to diagnose itself even if it is briefly claimable.

### 5.4 D2/locality — add proximity as a rankable field

Nothing in the agent record expresses locality today. Add an optional
`locality` string to the agent registration (source: `fleets.yaml` agent entry,
alongside `target`/`os`), e.g. `ovswarm/10.57.228.0-24`, `rocky/tailnet`.

Semantics are deliberately dumb: **string equality is "near", anything else is
"far".** No topology model, no distance metric. A repair task for a subject
records `metadata["repair_subject_locality"]`, and agents sharing that exact
string are the preferred cohort. This is enough to prefer a worker on the same
subnet as the sick host over one across a bastion, which is the whole point,
and it cannot rot into a wrong distance calculation.

### 5.5 The assignment rule — deferred widening, not a vote

A repair task is filed with:

```
metadata:
  repair_subject_agent_id:  agent_ovswarm-worker7
  repair_subject_locality:  ovswarm/10.57.228.0-24
  repair_preferred_until:   <filed_at + MAC_REPAIR_PREFERRED_WINDOW_SECONDS>
required_capabilities: {"repair"}
excluded_agent_ids: {agent_ovswarm-worker7}     # the subject itself
```

Allocator behaviour, in `classify_requirement_eligibility` alongside the
existing checks:

1. **Before `repair_preferred_until`** — reject agents whose `locality` does
   not equal `repair_subject_locality`, with a new *pin-class* (not
   exclusion-class) reason code `REPAIR_LOCALITY_DEFERRED`. Per the taxonomy
   comment at `allocator.py:940-952`, a pin-class code means "only another
   agent may run it — the task's own routing", which is exactly right here and
   keeps `unsatisfiable-requirements` from misreporting fleet capacity.
2. **After `repair_preferred_until`** — the locality check is skipped entirely;
   any `repair`-capable, non-excluded agent may claim.

Default window: 120s (`MAC_REPAIR_PREFERRED_WINDOW_SECONDS`). Rationale: agents
poll at `--poll-interval 2`, so 120s is ~60 poll cycles — ample for a near
agent to claim, negligible against the 900s lease and the hour-scale staleness
thresholds this whole subsystem operates on.

The result: the near, competent agent almost always wins; if none exists or
none is free, the task widens to the whole competent pool instead of deadlocking.
**No ballots, no quorum, no tie-break — the CAS still picks the single winner.**

### 5.6 D4 fix — structured failure classes and self-exclusion

Define a closed vocabulary, stored as `metadata["repair_failure_class"]` when a
repair task fails:

| Class | Meaning | Allocator consequence on re-file |
| --- | --- | --- |
| `unreachable` | claimer has no network path to the subject host | add claimer to `retry_excluded_agent_ids`; widen locality immediately |
| `no_privilege` | reached it, lacked rights to act (sudo, supervisor, credential) | add claimer to `retry_excluded_agent_ids` |
| `subject_gone` | host is genuinely dead/retired, not repairable | stop re-filing; escalate to human via `_notify_escalation` |
| `fix_attempted_symptom_persists` | acted, symptom remains | keep claimer eligible; carry prose forward as today |
| `timed_out` | lease expired with no terminal state | no exclusion (may have been unrelated load) |

The first two are the load-bearing ones: they are exactly the "put it back for
someone with better locality or privileges" behaviour from the premise, and
they are expressible entirely through `retry_excluded_agent_ids`, which the
allocator already honours (`allocator.py:953`).

Repair agents set the class when closing a task as failed. A failed close with
no class defaults to `fix_attempted_symptom_persists` (the safe, non-excluding
choice).

### 5.7 What goes on the bus, and why it is observation only

The premise wants agents to "discuss" over agentbus. Concretely, discussion
that changes *who acts* is what §5.1 rejects. What the bus should carry is
**visibility**, using schemas that already exist:

- `mac.task.lifecycle.v1` (`agentbus_schemas.py:153`) already broadcasts task
  state transitions. A repair task's create/claim/fail transitions therefore
  already appear on the bus with no new schema.
- A repair agent that is *about* to start invasive work (restarting a peer's
  supervisor, clearing a hold) SHOULD announce it via the existing
  `mac.agent.peer_message.v1` addressed to the subject's fleet, so a human or
  peer watching the bus sees "worker12 is repairing worker7" in real time.

This is announcement, not permission-seeking. Nothing blocks on a reply.
No new bus schema is required by this design — a deliberate constraint, since
`agentbus_schemas.py:4` records that topic names were previously "pure
convention: nothing" enforced them.

## 6. What this fixes, and what it does not

**Fixes:** a sick peer is detected (already), a repair task is filed with a real
capability requirement, offered first to a nearby competent agent, claimed by
exactly one of them, and on failure recycled with a machine-readable reason
that excludes the agents that structurally cannot succeed — converging instead
of thrashing.

**Does not fix:** the 2026-09-22 incident's *first* domino. The deploy died
silently and nothing noticed for 32 minutes until the fail-forward monitor ran.
Peer repair shortens the tail (holds get cleared without a human), but the
detection of an abandoned *deploy* still depends on the hub-side sentinel — see
§7.

## 7. Hub failover: the conflict that must be resolved before it is designed

The premise also asks: *"if the hub starts dying or becomes unreliable, another
agent should be prepared to take over the role of hub after an election."*

**This directly contradicts an accepted, documented decision.**
`docs/hub-availability.md`, under "What this deliberately does not do":

> **No automatic failover:** promoting is an operator action precisely because
> fencing (step 1) cannot be safely inferred by the standby from "I can't
> reach the hub".

and its split-brain rule:

> **the old hub must be fenced before the standby serves a single write.** Two
> live authorities silently diverge; nothing reconciles them after the fact.

That document's answer to hub availability is Postgres-level HA (streaming
replica + managed failover, with hub processes stateless against one DSN) plus
verified `mac-pg-backup` dumps, and a manual promote procedure whose *first*
step is fencing.

An election among agents cannot discharge that requirement. An agent that
cannot reach the hub cannot distinguish "hub is dead" from "I am partitioned
from a perfectly healthy hub" — and if it guesses wrong and promotes, the fleet
has two authorities, which the existing doc states nothing reconciles.

**Therefore this design does not propose hub election.** If it is to be
revisited, the prerequisite is not an election algorithm; it is a *fencing
mechanism* — something that can make the old hub provably unable to serve
writes without human action (STONITH-style power/network control, a Postgres
leader lease that the old hub cannot renew, or promotion delegated entirely to
a managed Postgres failover that owns the fence). Only once fencing exists does
"who promotes" become a question worth answering, and by then Postgres-managed
failover likely answers it.

There are, however, two unblocked and strictly-additive pieces worth doing.
Both are pure observability: they change no authority and require no fencing.

### 7.1 The watcher must not live only on the watched

`self_healing` and every
`diagnostics.py` check run hub-side. When the hub dies, the thing that notices
death dies with it. Agents should run a minimal outbound liveness check against
the hub and, on sustained failure, emit a local operator notification
(and a bus event once reachable again). That would have surfaced the 2026-09-22
incident in minutes. It should be specified separately from this document.

### 7.2 Foreseeable expiries are unmonitored, and the error misdirects

The same
"nothing was watching" shape shows up in a place that needs no consensus at all,
because the failure time is *known in advance*.

On 2026-09-23, mid-investigation, every `mac --profile hub-admin` call began
failing. The cause was a client credential that had lapsed ten hours earlier:
`jkh-hub-admin.v1`, issued `2026-08-24T09:24:24Z`, expired
`2026-09-23T09:24:24Z` — a routine 30-day lifetime (the shared default
`expires_in = 30 * 24 * 60 * 60` on both enroll and renew, `cli.py:8056,8073`).
Nothing warned beforehand. The admin path to the fleet simply went dead at a
timestamp that had been known for a month.

Two defects, both cheap to fix:

1. **No check exists.** `diagnostics.py` uses `expires_at` only for task leases
   (`expired-active-leases`, `diagnostics.py:233`). No check covers client
   credential expiry, so `mac admin diagnostics` reports a clean bill of health
   while the credential that runs it is hours from lapsing. A
   `credential-expiry` check warning at, say, T-7d is a near-exact clone of the
   existing lease check.

2. **The error names the wrong cause.** `client_principals.py:595` *skips*
   expired records while resolving a token (`continue`), so an expired
   credential never matches and the caller falls through to
   `api.py:2490` → `AuthorizationError("unknown bearer token")`. An expired
   credential is therefore indistinguishable from a forged or unknown one at
   the API surface. That message points an operator at token *drift* — the
   documented cause of 403s, with `mac admin fleet sync-token` as its
   remedy (`cli.py:10588`) — when the actual fix is `mac admin client renew`.
   Live consequence: the first response to this outage was to investigate token
   drift and inspect `sync-token`, which was the wrong repair path entirely.
   Distinguishing "expired at `<ts>`" from "unknown" costs one branch and is
   not a secret-disclosure risk: the caller already holds the token.

3. **The lifetime is a hardcoded literal, not a fleet setting.** All three
   issue paths bake in `30 * 24 * 60 * 60` as an argparse default
   (`cli.py:8005,8056,8073`). It is overridable only per-invocation with
   `--expires-in <seconds>`, which means the fleet's effective credential
   lifetime is "whatever the operator remembered to type". It should be a
   configured fleet policy (`MAC_CLIENT_CREDENTIAL_TTL_SECONDS`, with the
   30-day default preserved), so a fleet can shorten it deliberately — which is
   only safe once §7.3 exists.

This is not a one-off. Listing principals on the `rocky` hub on 2026-09-23
shows the pattern is already widespread:

| Client | `expires_at` | State on 2026-09-23 |
| --- | --- | --- |
| `jkh-ui` | 2026-08-26T20:56Z | **expired 28 days ago** |
| `jordanh-cxwwhggjx0` | 2026-09-17T03:23Z | **expired 6 days ago** |
| `jkh-yowza` | 2026-09-18T22:48Z | **expired 5 days ago** |
| `openclaw-fleet-upgrade` | 2026-09-24T22:30Z | **expires in ~26 hours** |
| `jkh-hub-admin` | 2026-10-23T20:17Z | renewed during this investigation |

Three credentials are already dead and one — belonging to a *fleet upgrade*
principal — lapses tomorrow. None of this is reported anywhere; it is visible
only by running `mac admin client list` and reading the timestamps by eye.

### 7.3 Rotation must be a renegotiation, not a cliff

The deeper defect is not the missing warning. It is that **expiry is treated as
an event that happens *to* the participants rather than something they
cooperatively manage.**

A hub and its workers hold a live, mutually-authenticated relationship. Both
sides know the credential's `expires_at` — it is in the principal record on one
side and the client profile on the other. There is no reason for that
relationship to end abruptly at a timestamp both parties can read in advance.
The correct behaviour is for the two to **renegotiate a fresh token well before
the current one lapses**, while the existing credential is still valid and the
channel still authenticates. Rotation should be a handshake over a working
connection, not a cliff that both sides walk off simultaneously.

Going deaf on rotation has a specific and corrosive second-order cost: **it
teaches operators to set effectively infinite lifetimes in self-defence.** If a
credential that expires is a credential that silently severs the fleet, then
every finite TTL is an outage waiting for a date, and the rational operator
response is to make the number enormous. The security property that expiry
exists to provide is then lost entirely — not because anyone decided short
credentials were wrong, but because the mechanism punished using them. Short
TTLs are only adoptable if renewal is automatic and invisible.

This also bounds the value of §7.2's warning. A T-7d alert is a fallback for
when renegotiation has failed; it is not the fix. A system that merely *warns*
before severing itself is still a system that severs itself.

**Required behaviour:**

- Each side tracks `expires_at` and begins renegotiation at a configurable
  fraction of remaining lifetime (proposed: `MAC_CREDENTIAL_RENEW_AT_FRACTION`,
  default `0.5` — halfway through the TTL, giving an equal span of retries
  before any hard failure).
- Renewal is authenticated by the *current, still-valid* credential. No
  out-of-band re-enrolment, no operator, no SSH.
- Renewal is idempotent and safe to retry: a worker that renews twice, or races
  a peer, converges on one valid credential rather than invalidating itself.
- The new credential is installed atomically — the mechanism already exists
  (`mac admin client profile install` writes via a backup-and-swap, observed
  emitting `"backup": ".../clients/backups/hub-admin.<ts>"`).
- A renegotiation that fails is loud **while the old credential still works** —
  that is the entire point of starting at 50% rather than at expiry.
- Hard expiry remains enforced. This changes *when the conversation about
  renewal happens*, not whether credentials expire.

**Explicit non-goal:** never-expiring credentials. The purpose of automatic
renegotiation is to make *short* lifetimes practical, which is the opposite of
the infinite-TTL workaround that the current cliff behaviour encourages.

This matters to the wider argument. A fleet that intends to repair itself
cannot be blind to the scheduled, arithmetically-predictable failures of its own
control path — and an error message that names the wrong cause will misdirect a
repairing *agent* exactly as reliably as it misdirected a human here. A fleet
that cannot keep its own credentials alive cannot be trusted to keep its agents
alive.

## 8. Implementation plan

Phased so each phase is independently landable and independently valuable.
Every phase must keep `scripts/run-contract-tests.sh` green.

**Phase 0 — stop the bleeding (no code).**
Set `MAC_SELF_HEAL_ENABLED=1` on the `ovswarm` hub (D3). Do **not** do this
before Phase 1 lands, or D2 will file 49 undispatchable tasks.

**Phase 1 — D2, the deadlock.**
Remove the `target_agent_id` pin from `_check_stale_deploy_hold`; introduce
`repair_subject_agent_id`; add the subject to `excluded_agent_ids`.
*Tests:* a stale-deploy-hold finding produces a task claimable by a healthy
peer and never by the held subject; assert `target_agent_id is None`.

**Phase 2 — D1, capabilities.**
Add the `repair` capability; thread `required_capabilities` through
`_file_fix_task`; classify which finding kinds require it; implement the
no-repair-agents-in-fleet fallback.
*Tests:* repair findings emit `{"repair"}`; a fleet with no repair-capable
agent still produces a claimable task; `unsatisfiable-requirements` stays clean.

**Phase 3 — D4, failure classes.**
Add the `repair_failure_class` vocabulary; populate
`retry_excluded_agent_ids` from `unreachable`/`no_privilege`; stop re-filing on
`subject_gone`.
*Tests:* an `unreachable` failure excludes that claimer from the re-filed task;
`subject_gone` escalates instead of looping.

**Phase 4 — locality and deferred widening.**
Add `locality` to agent registration and `fleets.yaml`; add
`REPAIR_LOCALITY_DEFERRED` as a **pin-class** reason; implement the
`repair_preferred_until` window.
*Tests:* a near agent claims inside the window; a far agent is deferred inside
it and claims after it; the deferral never renders a task unsatisfiable.

**Phase 5 — bus announcement.**
Emit `mac.agent.peer_message.v1` on repair start. No blocking, no new schema.

Phases 1–3 deliver most of the value and touch one file plus tests. Phase 4 is
the only one that changes the agent record shape and the allocator.

### 8b. Credential survivability track (§7.2 / §7.3)

Independent of the peer-repair phases above; may land in parallel. Ordered so
each step is safe on its own.

**C1 — Name the real cause.** Distinguish expired from unknown at the API
boundary: `client_principals.py:595` currently `continue`s past an expired
record, so the caller sees `api.py:2490` `"unknown bearer token"`. Return a
distinct error identifying expiry and the `expires_at` instant, and point the
remedy at `mac admin client renew` rather than letting the operator infer token
drift and reach for `mac admin fleet sync-token`.
*Test:* an expired credential produces an expiry-specific error, not the
unknown-token error.

**C2 — Report expiries.** Add a `credential-expiry` check to
`diagnostics.py`, warning at a configurable horizon (default 7 days) and
erroring once expired. Near-clone of `expired-active-leases`
(`diagnostics.py:233`).
*Test:* a principal expiring inside the horizon warns; an expired one errors;
a healthy one is silent.

**C3 — Make the lifetime a fleet setting.** Replace the hardcoded
`30 * 24 * 60 * 60` argparse defaults (`cli.py:8005,8056,8073`) with
`MAC_CLIENT_CREDENTIAL_TTL_SECONDS`, preserving 30 days as the default.
`--expires-in` continues to override per invocation.
*Test:* the env var changes the issued lifetime; absent it, 30 days; the flag
still wins over both.

**C4 — Renegotiate before the cliff.** The substantive item: hub and workers
refresh the session credential at `MAC_CREDENTIAL_RENEW_AT_FRACTION` (default
`0.5`) of its lifetime, authenticated by the still-valid credential, installed
atomically, idempotent under retry and races, and loud on failure while the old
credential still works.
*Tests:* a client past the fraction renews unattended and keeps serving across
the boundary; a renewal failure is reported while the old credential is still
valid; concurrent renewals converge on one valid credential; hard expiry is
still enforced for a client that never renews.

C1–C3 are small and independently useful. **C4 is the one that actually removes
the failure mode**, and the one that makes short TTLs adoptable instead of
something operators defend against by setting them enormous.

## 9. Alternatives considered

**A real election over agentbus (the literal premise).** Rejected in §5.1: it
duplicates `claim_task_v2`'s guarantee with a weaker mechanism and adds
split-brain modes the CAS does not have. The premise's *goal* — the right agent
fixes it, failures recycle to someone better, timeouts re-open the decision —
is fully met by capability + locality + exclusion + the existing lease expiry.

**Pin each repair task to a chosen agent at file time (hub picks the winner).**
Rejected: it reintroduces D2's failure shape. Any pin is a single point of
failure — if the chosen agent is busy, offline, or wrong, the task waits for a
lease expiry rather than being claimed by an available peer in seconds.

**A dedicated always-on repair daemon per host.** Rejected as premature: it is
a second execution path with its own credentials and lifecycle, when the task
ledger already provides durable, audited, retryable work distribution. Revisit
only if repair work proves unable to flow through normal dispatch.

**Auto-releasing stale roll-forward holds on a timer.** Explicitly rejected by
the existing remediation text (`self_healing.py:625-631`): *"Hold age does not
prove readiness; preserve the dispatch fence while recovery is incomplete."* A
hold released without verifying the node's generation and credentials puts a
half-deployed node back into dispatch. Repair must *verify then release*, which
is why it is a task for an agent and not a timer.

## 10. Open questions

1. **Who sets `repair` capability?** Proposed: `fleets.yaml` per-agent
   capabilities, same as `ops`/`python`. Needs confirming against the deploy's
   capability plumbing (`--capabilities` on the `mac-agent` argv).
2. **Does a repair agent need credentials it does not have today?** Restarting
   a peer's supervisor over SSH implies key material and sudo rights that the
   current worker provisioning may not grant. This may be the real constraint
   on how many agents can be `repair`-capable.
3. **Should `agent_unhealthy` require `repair` immediately?** 30 such tasks were
   filed and cancelled; adding a requirement no agent advertises would convert
   them from ignored to unsatisfiable. Phase 2's fallback covers this, but the
   first rollout should be watched.
4. **Locality string authorship** — hand-written in `fleets.yaml`, or derived
   from the agent's `target` at registration? Derivation is tempting and is how
   it rots; hand-written is honest but drifts after host swaps.

## Appendix: reproducing the evidence

```bash
# §3 — self-heal outcomes on rocky
mac --profile hub-admin task list --all --all-states --json \
  | python3 -c "import json,sys;from collections import Counter; \
      ts=json.load(sys.stdin); \
      sh=[t for t in ts if 'Self-heal' in str(t.get('title',''))]; \
      print(len(sh), Counter(t['state'] for t in sh).most_common())"

# §D3 — detector disabled on ovswarm, enabled on rocky
ssh horde@10.57.228.137 'grep MAC_SELF_HEAL_ENABLED ~/.mac/mac.env || echo NOT SET'
grep MAC_SELF_HEAL_ENABLED ~/.mac/mac.env

# §1 — the incident record
ssh horde@10.57.228.137 'cat ~/.mac/logs/fail-forward-20260922T131604Z.json'
mac --profile ovswarm --fleet ovswarm-hub admin diagnostics --check stale-dispatch-hold

# §7.2 — credential expiries are visible on demand but never surfaced,
# and no diagnostics check covers them
mac admin client list | python3 -c "import json,sys; \
  [print(c['id'], c['expires_at']) for c in json.load(sys.stdin)]"
mac admin diagnostics | python3 -c "import json,sys; \
  print([c for c in json.load(sys.stdin)['checks'] if 'cred' in c] or 'no credential check')"
```
