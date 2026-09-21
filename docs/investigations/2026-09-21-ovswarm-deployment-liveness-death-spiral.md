# Ovswarm deployment liveness death spiral RCCA, 2026-09-21

Status: recovery incomplete at the evidence cutoff. The durable cohort journal and dispatch holds remain authoritative. This report does not claim that the fleet is recovered, release-qualified, or serving work.

Source under investigation: `7f3bbd12a0badb4f4812749bd682476ed85bb3ce`.

## Executive summary

A deployment of the 50-worker `ovswarm` fleet failed because MAC turns bounded, recoverable node defects into a fleet-wide stop and then makes recovery depend on a long sequence of individually reliable operations. Worker-local identity drift was the trigger, not the central root cause.

Two different drift shapes exposed the same architectural problem. An earlier recovery found an early-cohort worker without `~/.mac/mac.env`; the old startup-hold writer silently did nothing, after which attestation recovery created an incomplete environment. That defect is fixed in `7f3bbd12`. The next rollout reached the final cohort worker, which lacked `~/.mac/deployed-source-revision` but retained a full installed `mac.env`. The strict retained-successor classifier rejected that inconsistent identity, correctly failing closed at the node boundary but only after the controller had serially quiesced nearly the whole fleet.

The long serialized quiescence and reverse-recovery paths then interacted with the hub's one-hour default fungible-agent TTL. Workers intentionally stopped by the deployment could not heartbeat, while expiry knows about task leases but not deployment-epoch membership. Rows needed by recovery could therefore be tombstoned as if their hosts had departed. Finally, one transient SSH connection timeout to the hub stopped the serialized recovery pass. MAC correctly retained the journal and dispatch holds, but the fleet remained globally unavailable.

The architectural correction is not to weaken identity, attestation, filesystem, or epoch fences. It is to move tolerance and isolation to the right boundaries: accept and classify safe historical input, generate one exact canonical state, quarantine incoherent nodes before mutation, deploy in bounded rolling cohorts with an availability floor, pin active epoch members against liveness expiry, and retry idempotent journal-bound operations. Safety without bounded progress is not a complete safety property on flaky infrastructure.

## Evidence basis

The operational facts come from the live controller output and retained recovery evidence: the named workers, worker50's exact rejection, the hub SSH timeout, and the final statement that cohort recovery remained incomplete with journal and holds retained. The mechanism and line references come from the exact `7f3bbd12` source and its tests. Conclusions about causality are limited to where those two bodies of evidence agree. This document does not infer successful recovery from process liveness, a partial node count, or the existence of a journal.

## Impact

- The requested 50-worker rollout did not converge.
- The deployment stopped at worker50's phase-1 quiescence with `retained successor has an installed identity`.
- Reverse recovery later stopped on `Connection to 10.57.228.137 port 22 timed out`.
- The cohort journal remained non-terminal and deployment/dispatch holds remained in place. Work could not safely be released.
- Earlier long-running attempts also allowed fungible worker rows needed by recovery to age past their liveness TTL and be tombstoned.
- No data-loss or unsafe-dispatch claim is supported by the evidence. The fail-closed journal and holds prevented an ambiguous fleet from accepting work.

## Timeline

The exact wall-clock timestamps remain in the deployment logs and journal; this sequence records only facts established in the live operation and source.

1. A retained recovery pass encountered an early-cohort worker without `~/.mac/mac.env`.
2. The then-current startup-hold setter returned success without writing `MAC_STARTUP_CLEAR_HOLD=0` when the file was absent.
3. Missing-key recovery created `mac.env` with only `MAC_ATTESTATION_KEY`. The retained-successor identity contract rejected the incomplete environment.
4. The hold writer was corrected, tested, and incorporated into source `7f3bbd12`.
5. A new 50-worker typed rollout from `7f3bbd12` began. Read-only preparation and staging used bounded fan-out, while service quiescence remained cohort-ordered and serial.
6. Workers before worker50 were quiesced. Worker50 then failed phase-1 quiescence with `retained successor has an installed identity`.
7. Inspection established the trigger shape: worker50 had no `~/.mac/deployed-source-revision` but did have a full installed `~/.mac/mac.env`.
8. The controller entered retain-forward recovery. Recovery processed candidates serially.
9. During these long stopped intervals, the hub's fungible-agent expiry policy could tombstone workers silent for its default 3,600-second TTL because deployment-epoch participation is not an expiry exemption.
10. A later recovery operation made a direct SSH connection to the hub at `10.57.228.137`; one connection attempt timed out.
11. Recovery returned failure before the journal could record every node as recovered. The journal and holds were retained for adoption by the next controller.

## Trigger versus root cause

### Immediate trigger

Worker50 presented contradictory installed-state evidence:

- `~/.mac/deployed-source-revision`: absent;
- `~/.mac/mac.env`: present and carrying full installed identity, not the two-key recovery-only form;
- phase-1 retained-successor authority: present, making the partial-successor path otherwise eligible.

At `deploy/fleet-node-install.sh:7511-7533`, `prove_prepared_cli_without_gateway` uses existence of `deployed-source-revision` as the discriminator between an installed deployment and a retained partial successor. With the marker absent, it calls `prove_phase1_prepared_cli_authority` (`7418-7508`). That proof permits an environment only when the key set is exactly `MAC_ATTESTATION_KEY` and `MAC_STARTUP_CLEAR_HOLD`, with a valid key and hold clearing disabled (`7342-7414`). A full installed environment therefore produces the observed error.

This rejection is preferable to treating an ambiguous identity as trusted. The defect is that contradictory evidence was discovered at worker50, after cohort-wide mutation, and had no bounded repair or quarantine path.

### Architectural root cause

MAC couples the safety fate and recovery liveness of the entire selected fleet to every individual node and every individual transport operation.

The protocol makes exactness global where it should make exactness local:

- one node's historical marker drift stops a cohort after other nodes have already stopped;
- recovery walks every candidate serially, so elapsed time and transient-failure probability grow with fleet size;
- a stopped worker's inability to heartbeat is interpreted by an independent TTL subsystem as host departure;
- a single retryable SSH failure stops recovery and leaves all deployment-owned holds in force;
- there is no availability budget limiting how many otherwise healthy workers may be quiesced before the next proof point.

Each rule is defensible in isolation. Together, on a flaky network and a 50-node fleet, they create a liveness death spiral: conservative stop increases elapsed time; elapsed time triggers liveness expiry; expiry removes identities required for recovery; recovery becomes longer and more fragile; one transient failure preserves the global stop.

The primary root cause is therefore over-strict global coupling without bounded-progress, isolation, or availability invariants. The early-cohort worker's absent environment and the final cohort worker's marker/environment drift were initiating conditions.

## Violated invariants

### Recovery hold precedes recovery identity

Before attestation recovery may create or update `mac.env`, the node must durably carry `MAC_STARTUP_CLEAR_HOLD=0`. The old absent-file behavior violated this invariant on the early-cohort worker.

`7f3bbd12` fixes it in `deploy/deploy-mac-fleet.sh:7985-8119`. `retain_remote_generation_for_forward_repair` invokes the setter before attestation reconciliation (`14423-14436`). The regression at `tests/test_deploy_attestation_recovery.py:72-95` executes the production shell setter and real attestation installer, proving the final environment contains exactly the two recovery keys. Preservation and symlink rejection are covered at `98-130`.

### Installed identity is coherent before mutation

The revision marker, environment identity class, installed source and venv, generation/barrier, and signed deployment manifests must describe one state before the hub epoch opens. Worker50 violated this invariant, but MAC did not diagnose it until phase-1 quiescence.

The installed-static-hub correction already included in `7f3bbd12` handles the coherent case where `deployed-source-revision` exists (`tests/test_fleet_node_daemon_quiescence.py:1199-1238`). It deliberately does not authorize marker-absent/full-environment drift.

### Deployment-epoch membership outranks ordinary heartbeat expiry

A worker intentionally stopped inside a live deployment epoch is not evidence of a departed host. It must remain addressable until the epoch is terminal or an explicit fenced action removes it.

The current expiry policy gives fungible agents a default TTL of 3,600 seconds (`src/mac/services.py:18714-18745`). `expire_ephemeral_agents` skips an active task lease, but not a deployment epoch or deployment-owned hold, before tombstoning the row through `hub-ephemeral-expiry` (`18770-18808`).

### Fleet operations have bounded blast radius and duration

Read-only preparation and immutable staging use `run_bounded_node_phase` with bounded fan-out (`deploy/deploy-mac-fleet.sh:11524-11610`). Service quiescence is explicitly serialized (`16669-16698`), as is retain-forward recovery (`15417-15426`). The maximum unavailability and recovery time therefore scale linearly with fleet size and can exceed the TTL that governs the same workers.

### Retryable transport failure does not become protocol failure

Recovery operations are journal-bound and designed to be replayed, but attestation reconciliation uses multiple direct `ssh` calls with a ten-second connect timeout and immediate error propagation (`deploy/deploy-mac-fleet.sh:12534-12776`). `recover_cohort_node` returns before its `aborted-node` transition when one of those calls fails (`14582-14775`). A transient connection failure therefore stops the entire pass.

### Incomplete recovery remains fail closed

This invariant held. The controller left the durable journal non-terminal and retained dispatch holds. `recover_incomplete_cohort_transaction_before_deploy` discovers and adopts a dead controller's exact journal revision before replay (`15617-15677`). A later normal typed deployment can resume it; there is no supported recovery-only CLI mode.

## Contributing factors

- A single marker is overloaded as the installed-versus-partial-successor discriminator even when other installed identity is present.
- Identity coherence is checked inside the mutating phase instead of across every selected node in the read-only preflight barrier.
- The all-selected release contract correctly prevents a partial success from being called complete, but there is no separate per-node quarantine outcome that preserves healthy serving capacity.
- Journal mutations are sequential and remote recovery is performed in the same loop, coupling durable ordering to network latency.
- The liveness sweeper and deployment controller do not share an explicit epoch-membership lease.
- The one-hour fungible TTL is reasonable for unattended capacity cleanup but is not compared with worst-case deployment and recovery duration.
- Direct SSH defaults are fail-fast but have no bounded retry for operations whose journal operation IDs and deployment fences make replay safe.
- The failure text `retained successor has an installed identity` identifies the strict contract violation but not the contradictory artifacts or safe remediation.

## What worked

- Filesystem, attestation, and identity checks failed closed. The response did not weaken modes, ownership, no-symlink rules, exact key sets, signatures, or deployment fencing.
- The typed cohort journal preserved exact recovery authority across controller death and network failure.
- Dispatch holds remained in place, preventing ambiguous workers from taking new work.
- Phase-1 restore contracts retained enough generation-bound evidence for diagnosis and replay.
- `7f3bbd12` corrected the early-cohort absent-environment defect with atomic owner-private writes and production-level tests.
- The installed static-hub classifier correctly keeps coherent installed nodes out of the partial-successor exception.
- Read-only preparation, prerequisite proof, staging, and later immutable phases already have a reusable bounded-fan-out primitive.

## Postel-like tolerance without weaker security

The controller should be liberal in the safe historical forms it can *recognize*, and conservative in the one state it *emits*. This is narrower than accepting arbitrary environment syntax or guessing identity.

Input tolerance may include known prior canonical environment layouts, a missing redundant marker when authenticated generation-bound manifests independently prove its exact value, and explicitly versioned historical receipts. A read-only decoder should classify and normalize these forms into a typed internal state. Ambiguous, unowned, writable, symlinked, multiply linked, unbounded, unsigned, or conflicting artifacts must still fail closed.

Generated state remains exact: one canonical `mac.env` rendering, one revision-marker format, owner-only regular files, atomic replacement, fsync, exact schemas, exact epoch and generation binding, and no secret-bearing diagnostics. Tolerance belongs before the trust decision; canonicalization belongs behind an authenticated repair fence. Postel-like handling must not mean sourcing unknown shell, accepting extra recovery keys, or inferring a revision from an untrusted checkout.

## Fixed and open

### Fixed in `7f3bbd12`

- An absent `mac.env` is atomically created with startup hold clearing disabled.
- Existing assignments are preserved while duplicate startup-hold assignments are reduced to one canonical value.
- Unsafe parent, file, lock, or symlink paths fail closed.
- Real attestation installation preserves the exact recovery hold and attestation keys.
- A coherent installed static hub with a deployed-revision marker uses ordinary quiescence rather than the partial-successor exception.

### Still open

- Worker50's marker-absent/full-environment state has no pre-mutation classification or authenticated repair.
- Invalid nodes cannot be quarantined independently while an explicitly approved healthy cohort remains available.
- Quiescence and recovery have fleet-size-linear remote critical paths.
- Fungible expiry is unaware of active deployment-epoch membership.
- Journal-idempotent SSH operations do not retry bounded transient failures.
- There is no preflight comparison between worst-case operation duration and liveness/credential budgets.
- Error evidence does not name the exact contradictory identity surfaces.

## Corrective and preventive actions

### P0 — required before another 50-node all-selected rollout

1. **Add an all-node identity-coherence preflight.** Before hub epoch open or the first service stop, classify the revision marker, environment class, source/venv, generation barrier, and signed manifests for every selected node. Report exact contradictions. Quarantine worker50-shaped nodes before mutation.
2. **Add authenticated marker repair, not classifier relaxation.** Reconstruct a missing `deployed-source-revision` only when journal-bound or signed deployment manifests agree on the exact revision and generation. Write it atomically under the deployment fence. Extra keys must remain forbidden in a true recovery-only environment.
3. **Pin live epoch members against expiry.** Record a durable deployment-epoch membership lease in the hub. `expire_ephemeral_agents` must skip an exact active member until the epoch is terminal, while continuing to tombstone unrelated silent fungible agents.
4. **Retry idempotent recovery transport.** Add bounded exponential backoff for hub and node SSH operations whose operation ID, endpoint identity, and deployment lock make replay safe. Revalidate route identity and fence before every attempt. Endpoint changes remain a hard stop.
5. **Recover and prove the current epoch before release.** Adopt the retained journal through the normal typed deployment entry point, reach a terminal journal state, account for every retained hold, and independently verify all registered workers before any canary release.

### P1 — remove the fleet-size liveness amplifier

1. **Use bounded rolling cohorts with an availability floor.** For 50 workers, default to batches no larger than five and keep at least 45 healthy, eligible workers until the next batch is proven. An all-50 qualification may require every batch eventually, but it must not require all 50 to be down together.
2. **Decouple WAL ordering from remote serialization.** Durably arm a batch of per-node intents, perform fenced remote operations concurrently with bounded fan-out, then serialize or batch-CAS completion records. Apply the same design to reverse recovery.
3. **Make quarantine explicit.** A node with incoherent but contained local state becomes `deployment_quarantined`, remains dispatch-held, and receives a typed repair plan. Its defect must not silently shrink an all-selected acceptance denominator, but it also must not stop healthy nodes from serving.
4. **Enforce a duration budget.** Before mutation, compare cohort size and phase bounds with ephemeral TTLs, credential lifetimes, hold leases, and availability policy. Refuse an unsafe plan or reduce its batch size.
5. **Add fleet-scale fault tests and telemetry.** Exercise 50-node delayed operations, marker drift, TTL advancement, controller death, and transient SSH loss. Emit per-batch availability, oldest heartbeat, epoch-pin age, recovery progress, retries, quarantines, and estimated time remaining.

## Validation and exit criteria

The incident may be closed only when all applicable criteria are evidenced; a green unit test or a terminal journal alone is insufficient.

### Current-fleet recovery

- The retained journal is adopted by one controller and reaches a terminal state without manual database edits.
- Every journal-pinned agent identity is present or is restored through an explicit journal-bound resurrection path; no required row remains tombstoned.
- Every deployment-owned hold, successor hold, and pre-existing operator hold is accounted for. Only the intended holds are released.
- All 50 registered targets resolve from the frozen fleet registry and independently prove the expected source revision, deployment generation, service health, and attestation identity.
- One held canary completes before staged release of the wider canary campaign. Release qualification still requires the separately defined all-50 participation evidence.

### Identity classification and repair

- An integration test reproduces the early-cohort worker's absent environment and proves the hold exists before key installation.
- An integration test reproduces worker50's absent marker plus full installed environment and fails during read-only preflight, before hub epoch open or any service stop.
- Positive repair tests accept only mutually agreeing signed/journal-bound evidence and emit the exact canonical marker.
- Negative tests reject stale revision, wrong generation, unsigned evidence, unsafe file types/modes/owners, extra recovery keys, and concurrent artifact changes.

### Liveness and availability

- With virtual time advanced beyond 3,600 seconds, an active epoch member remains registered while an unrelated silent fungible agent expires normally.
- A 50-worker deployment never has more than the configured batch size unavailable and maintains the configured availability floor at every observable checkpoint.
- Quiescence and recovery wall time are bounded by batch phase limits rather than `worker_count × per-node timeout`.
- Killing the controller after intent, after remote mutation, and before completion publication converges through journal adoption without duplicate mutation.

### Retry and idempotency

- A first-attempt hub SSH timeout followed by recovery succeeds without rotating a key twice, releasing a hold twice, or publishing duplicate journal transitions.
- Exhausted retries leave the same journal and hold authority intact and provide the exact next replay command and failed endpoint identity.
- An endpoint identity change during retry fails closed and cannot reuse the prior node's authority.

## Scope and non-goals

This RCCA does not recommend removing the typed cohort journal, exact attestation-key contracts, owner-only filesystem checks, endpoint identity, deployment locks, signed evidence, all-selected acceptance semantics, or dispatch holds. Those controls limited the damage.

It recommends changing their composition. Per-node defects should be contained per node; global acceptance should remain honest; rolling service availability should be a first-class invariant; and journal-bound recovery should make bounded forward progress despite ordinary transient infrastructure failure.
