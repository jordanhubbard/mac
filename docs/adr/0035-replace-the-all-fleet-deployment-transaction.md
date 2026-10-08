# ADR 0035: The all-fleet deployment transaction is replaced, not extended

- Status: Proposed
- Date: 2026-09-23
- Context: `docs/investigations/2026-09-21-ovswarm-deployment-liveness-death-spiral.md`

## Context

A 50-worker rollout did not converge. The controller stopped at one node's
phase-1 quiescence, entered retain-forward recovery, and a single SSH connection
timeout ended the serialized recovery pass. The fleet was left globally
unavailable with its journal non-terminal and every deployment-owned hold in
force.

Nothing in that sequence was a bug in the ordinary sense. Each rule is
defensible alone: fail closed on contradictory node identity, stop the cohort
rather than proceed ambiguously, retain the journal rather than guess, expire
agents that stop heartbeating. The RCCA names what they do together:

> conservative stop increases elapsed time; elapsed time triggers liveness
> expiry; expiry removes identities required for recovery; recovery becomes
> longer and more fragile; one transient failure preserves the global stop

and draws the conclusion this ADR exists to record:

> Safety without bounded progress is not a complete safety property on flaky
> infrastructure.

### The incremental approach was tried, and is measurable

Between 2026-09-20 and 2026-09-22 the deployment produced **15 retained
fail-forward records**. Every one has the identical shape: the static hub node,
`post_manifest_status: absent`, `installed_revision: ""`, `rollback_performed:
false`. Each carries a *different* source commit, and those commits are
sequential trunk commits whose own messages are the remediation:

- `Require valid onboarding operator evidence`
- `Preserve onboarding repair helper stdin`
- `Make retained epoch stop tolerate absent unit`
- `Accept retained attestation recovery identity`
- `Bootstrap retained recovery hold environment`
- `Document ovswarm deployment liveness RCCA`

That is the load-bearing evidence. For three days the fix for the deployment was
delivered *by* the deployment, one commit at a time, and the delivery kept
failing. A repair path that depends on the mechanism under repair is not a
repair path.

Targeted fixes have since landed and the fleet is currently quiet:

- `Pin release epoch identities against TTL expiry (#873)` — `expire_ephemeral_agents`
  now skips members of an `open`/`proved` `fleet_release_epochs` row.
- `Parallelize fleet deployment recovery (#886)`.
- `Decouple worker credential rotation from deploy (#875)`.
- `run_journal_bound_recovery_with_retry()` for journal-bound recovery transport.

Each is a guard on one known case. `expire_ephemeral_agents` still tombstones on
heartbeat silence — it simply knows about one exemption now. Any future
long-stop path that is not epoch-tagged reproduces the original failure exactly.
The last rollout succeeded, but one success on a mechanism that failed fifteen
times is not evidence of convergence.

### The cost of the current state, measured

The decisive fact is not the incident. It is what the fleet has produced since.

Fleet-authored commits on trunk, by month:

| Month | Fleet-authored commits |
| --- | --- |
| 2026-06 | 14 |
| **2026-07** | **737** |
| 2026-08 | 15 |
| 2026-09 | 27 |

That is a ~98% collapse after July, and the composition matters more than the
count. **Every fleet-authored commit since 2026-09-14 is the fleet repairing its
own deployment machinery** — onboarding evidence, retained successors,
attestation recovery, hub bootstrap, hold environments, route preservation, and
the RCCA itself. None is product work. Before that run there is a twenty-day
silence (2026-08-25 to 2026-09-14).

The ledger's terminal-state distribution points the same way: 5,243 `cancelled`,
2,868 `failed`, 2,265 `completed` — more than twice as much work cancelled as
completed.

> **Correction (2026-09-24).** The first version of this section also claimed
> that "zero of 2,265 tasks in `completed` state carry a `completed_at`
> timestamp". **That claim was false and is withdrawn.** It came from reading
> `mac task list --json`, whose rows omit the `completed_at` field entirely;
> `mac task show` returns it populated. The error does not affect the decision,
> which rests on the commit-history collapse above — independently verifiable
> from git and not from the ledger — but a decision record must not carry a
> measurement it cannot support.

A fleet of fifty workers has therefore spent roughly two months producing
documentation about why it cannot deploy. Any assessment of this decision that
treats the current transaction as a working system to be protected is measuring
against a baseline that does not exist.

## Decision

**The all-fleet deployment transaction is replaced rather than extended.
Compatibility with its current shape is explicitly not a constraint.**

Five capabilities are *deleted*, not made configurable, not deprecated behind a
flag:

1. **Heartbeat-driven identity deletion.** TTL expiry transitions a durable
   worker to `offline`. Only an explicit fenced decommission may delete dispatch
   identity, and decommission is refused while any epoch, hold, journal, task, or
   lease references the worker.
2. **Credential rotation inside software deployment.** Deployment validates the
   current credential. Rotation becomes a separate per-node state machine with
   old/new overlap until the successor heartbeat is proved.
3. **Ambient route discovery after planning.** One signed, hashed route bundle is
   frozen from the fleet registry; every subprocess and receipt in the epoch names
   its digest.
4. **Post-quiescence network dependencies.** A content-addressed capsule — source,
   toolchains, wheels, runtime images, helpers, service definitions, recovery
   material — is pre-staged before the barrier. After it, outbound DNS, download,
   and connect attempts are protocol violations.
5. **The all-selected serial controller path.** Idempotent per-node state machines
   run in parallel lanes of at most eight. A failed lane stays held; successful
   lanes commit independently; one slow node may block only its own lane.

The organising invariant: **the worst-case stopped window must be bounded and
independent of fleet size**, and must be compared against the liveness and
credential budgets governing the same workers *before* a rollout begins.

### What is explicitly not weakened

The correction is not to relax identity, attestation, filesystem, or epoch
fences. Those fences worked: they failed closed, preserved an exact recovery
journal across controller death, and kept an ambiguous fleet from accepting work.
Tolerance belongs *before* the trust decision — recognise safe historical forms,
emit one canonical state — never at it. Ambiguous, unowned, writable, symlinked,
unsigned, or conflicting artifacts continue to fail closed.

## Consequences

- Failure becomes local. One drifted node no longer decides the fate of 49
  healthy ones, because lanes commit independently and an availability floor caps
  how much of the fleet may be stopped at once.
- Identity survives silence, so recovery stops depending on re-registering rows
  that cleanup removed — the step that turned routine TTL expiry into a global
  lock.
- The stopped window shrinks and stops scaling with fleet size, which is the
  variable the entire incident turned on.
- Rollouts become *diagnosable before* mutation: contradictory node identity is
  classified in a read-only preflight rather than discovered at the first stop.
- Cost: capabilities disappear. Deployment can no longer rotate credentials, and
  nothing may resolve a route after planning. Callers relying on either must move
  to the replacement state machines.
- Cost: pre-staging a full capsule raises the storage and bandwidth cost of every
  deployment, and moves failures earlier, where they are cheaper but more
  frequent.

### On the risk of doing this

The obvious objection is that replacing a deployment transaction risks breaking
it. That objection assumes a working baseline, and the measured throughput above
shows there is not one. **Regression risk is bounded by current output, and
current output is approximately zero.** A fleet that has produced no product work
in two months cannot be made materially less productive by a rewrite; the
downside is engineering effort, not lost delivery.

The symmetric cost is rarely written down, so it is written down here: **not
doing this preserves a known, continuing loss.** The status quo has already cost
one fifty-worker fleet its entire productive output since July, and it requires an
operator for every rollout. Declining to act is not the conservative choice. It is
a choice to keep paying that, indefinitely, in exchange for avoiding a risk to
throughput that does not currently exist.

The genuine risks are narrower and worth stating precisely:

- The targeted fixes already landed (`#873`, `#875`, `#886`, the journal-bound
  retry helper) encode real, hard-won knowledge about specific failure modes. The
  replacement must subsume that behaviour rather than discard it, and those fixes
  stay in force until it demonstrably does.
- The replacement must land behind the same contract gates. Nothing here licenses
  weaker verification.
- A rewrite can restore throughput and still be wrong about *why* it failed. The
  P0 preflight work matters independently, because it is what converts a silent
  late failure into an early diagnosable one.

## Alternatives considered

**Continue incremental hardening.** Rejected, and this is the alternative with
the most evidence against it, because it is not hypothetical — it is what has
been happening.

Every fleet-authored commit since 2026-09-14 is an increment of exactly this
strategy: `Require valid onboarding operator evidence`, `Preserve onboarding
repair helper stdin`, `Accept retained attestation recovery identity`, `Make
retained epoch stop tolerate absent unit`, `Bootstrap retained recovery hold
environment`, `Quiesce installed static hubs normally`, `Repair retained
first-install fleet successors`. Each is a correct fix for a real defect. The
strategy has now been run for six weeks, has consumed essentially the fleet's
entire recent output, and has produced no product work — only more deployment
repair.

Fifteen of those increments were themselves delivered by the failing mechanism
and failed in delivery. None addressed the coupling that makes one node's drift a
fleet-wide outage, because that coupling is the design, not a defect within it.
The RCCA states the conclusion directly: incremental compatibility with the
all-fleet transaction preserves the wrong abstraction.

The question is not whether the next increment would also be correct. It would
be. The question is whether a sequence of correct increments is converging, and
the throughput data says it is not.

**Weaken the identity and attestation fences so fewer nodes fail closed.**
Rejected, and worth recording as rejected: it would trade a liveness problem for
a correctness problem. The fences were the part that behaved well. Ambiguity
admitted at the trust boundary is unrecoverable in a way that a stalled rollout
is not.

**Operational workaround: stop doing all-selected rollouts; deploy in manual
lanes.** Rejected as a standing answer, though it is exactly what the incident
recovery did by hand (six independent eight-node lanes). Encoding that shape into
the controller is replacement item 5; leaving it as operator discipline means the
next rollout under time pressure reproduces the incident.

**Raise the fungible TTL above worst-case deployment duration.** Rejected: it
tunes one constant against an unbounded quantity. Recovery time scales with fleet
size, so any fixed TTL is eventually exceeded, and the failure returns with a
larger fleet rather than being removed.

## Tracking

| Item | Task |
| --- | --- |
| Replacement 1 — TTL marks offline; fenced decommission only | `task_4dcbce8fdfd04c95b3f8c062f9a4fe9e` |
| Replacement 2 — credential rotation out of deploy | landed in `Decouple worker credential rotation from deploy (#875)` |
| Replacement 3 — one signed route bundle | `task_4baaa28f8d3349d18d21493611336ef9` (P0-8) |
| Replacement 4 — pre-staged capsule, no post-barrier network | `task_8d9c292e7bce4289a63f99fa6d3abfaf` (P0-7) |
| Replacement 5 — bounded parallel lanes, availability floor | `task_ba8945b410bd49bb86b0396b5a270b50` |

The RCCA's P0 list is gating for any further all-selected rollout and is tracked
separately: `task_ca131e86e14f4832b31d7e12555e564f` (identity-coherence
preflight), `task_9dec42383a6b476c959b2304dea0ded1` (authenticated marker
repair), `task_e95fbb49792b45a9bb955d0959651022` (monotonic abort for untouched
participants).
