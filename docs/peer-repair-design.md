# Peer Repair: agents repairing agents

- Status: **Proposed** (not accepted; no code written)
- Date: 2026-09-22
- Scope: agent-to-agent repair. Hub failover is surveyed in §7 and
  deliberately **not** proposed — it conflicts with an existing accepted
  decision, and that conflict is the finding, not an oversight.
- Related: `docs/hub-availability.md`, ADR 0013 (authoritative hub allocator),
  ADR 0014 (visibility is not a dispatch gate)

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

There is, however, an unblocked and strictly-additive piece worth doing:

**The watcher must not live only on the watched.** `self_healing` and every
`diagnostics.py` check run hub-side. When the hub dies, the thing that notices
death dies with it. Agents should run a minimal outbound liveness check against
the hub and, on sustained failure, emit a local operator notification
(and a bus event once reachable again). That is pure observability — it changes
no authority, requires no fencing, and would have surfaced the 2026-09-22
incident in minutes. It should be specified separately from this document.

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
```
