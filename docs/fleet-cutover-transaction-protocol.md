# Fleet Cut-over Transaction Protocol

Status: implemented contract for `mac.fleet_cutover_transaction.v2`. Production
publication remains frozen until the repository gates and composed fault matrix
in this document pass. The deployment shell is the bounded transport adapter;
the durable journal, typed node receipts, and hub epoch service are the
transaction authorities.

## Objective

A cut-over of a selected failure-domain cohort must converge to exactly one of
two outcomes:

1. the exact prior, authenticating node generations remain held; or
2. the exact successor generation is durably committed for the complete cohort,
   remains held by the requested successor authority, and all finalizers
   eventually complete.

No crash, timeout, target swap, replay, or partial participant failure may
produce an untracked hybrid. Synchronization is a logical barrier. It does not
require equal node speed or synchronized wall clocks.

Normal rollouts partition the heterogeneous fleet by supervisor family. The
systemd, launchd, and supervisord cohorts commit independently against the same
reviewed release identity, so a manager-specific failure blocks only that
manager's lane. Mixed-supervisor cohorts are reserved for changes whose safety
really requires one fleet-wide atomic outcome.

## Ownership rule

Every cut-over mutator must declare one classification before it can run:

| Class | Meaning | Failure rule |
| --- | --- | --- |
| `transactional` | Exact prior state is captured and an idempotent compensation is armed before mutation | Abort restores the prior state and records a receipt |
| `commit_staged` | Prior and successor authorities coexist during prepare; one is selected by the hub commit | Abort discards the successor; commit retires the prior |
| `monotonic_prerequisite` | Compatible additive preparation completed before the cohort transaction opens | Failure prevents prepare; cohort rollback never removes it |
| `external` | State is owned by another authority and is observed but not mutated | Identity or readiness drift fails closed |
| `ephemeral` | Temporary state has no authority after its bounded operation | Cleanup is idempotent and cannot determine commit |

Unclassified mutation is forbidden in synchronized mode.

The initial inventory is:

| Surface | Class and owner |
| --- | --- |
| Source, virtualenv, executables, runtime environment, service definitions, supervisor topology, managed gateway state | `transactional`, node participant |
| Worker principals, attestation candidate, report-executor approval, identity policy revision | `commit_staged`, hub epoch authority |
| Dispatch holds, service-claim withdrawal, release marker | `commit_staged`, hub epoch authority |
| Package installation, image pulls/caches, schema-compatible migrations | `monotonic_prerequisite`, onboarding or preflight |
| Reverse tunnels, SSH authorization, shared Qdrant/Firecrawl/WebDAV services | separate infrastructure prerequisite unless represented by a typed transactional participant |
| Fleet registry route | `external`; route adapter proves it reaches the journal-bound resource identity |
| Logs, test output, temporary relays and control sockets | `ephemeral` or retained audit evidence |

Hermes/OpenClaw state must be split explicitly into transactional configuration,
monotonic durable user data, and ephemeral runtime data. A recursive directory
copy is not an acceptable substitute for that classification.

## Authorities

### Hub epoch authority

The hub database is authoritative for hub-owned resources. It exposes typed,
idempotent `open`, `prove`, `commit`, `abort`, and `status` operations for one
exact epoch identity.

Open atomically, before any node mutation:

- binds the ordered cohort and exact request digest;
- snapshots each selected dispatch hold and reason;
- claims one open epoch per agent;
- binds an already issued but not yet installed pending worker principal without
  superseding the old active principal;
- stages an attestation candidate without replacing the current key;
- stages the desired report-executor approval and policy revision; and
- returns only secret-free identities and digests.

Pending worker principals may authenticate during preparation, but they are not
promoted and old principals are not retired. Attestation candidates are proved
directly while the current key remains authoritative. Open does not require a
destination install receipt, authenticated successor heartbeat, or successor
startup proof; requiring any of those would move node environment mutation
ahead of the rollback-intent boundary.

After a node's phase-2 rollback intent is armed and its successor is applied,
prove accepts the complete cohort's exact install receipts, pending-principal
heartbeats, candidate-key challenges, generation evidence, and report-executor
startup evidence. It persists only secret-free proof material and advances the
hub epoch from `open` to `proved`. Partial proof never authorizes commit.

Commit is one database transaction. It revalidates the complete cohort,
generation proofs, pending principals, attestation proofs, approvals, policy,
holds, and absence of active work. It then promotes the staged identity bundle,
transitions every dispatch hold, withdraws service claims, and writes the exact
epoch marker. Once that marker exists, node rollback is forbidden.

Abort discards only epoch-owned pending state and restores or retains the exact
snapshotted holds. It never revokes an unrelated principal, key, approval, or
operator hold.

Status is read-only and returns `absent`, `open`, `proved`, `committed`,
`aborted`, or `mismatch` for the supplied exact identity digest. Transport
failure is never interpreted as `absent`.

The controller persists each secret-bearing open or prove request separately
from the secret-free journal plan. The owner-private replay envelope is stored
on the hub, is bound to the exact epoch and operation, and is removed after its
receipt is journaled or the epoch is aborted. Recovery may replay only that
envelope; credentials and candidate keys never enter the cohort journal.

### Node participant

Each node exposes these idempotent operations with typed, owner-private,
generation-bound receipts:

1. `identify`
2. `arm-phase1`
3. `quiesce`
4. `restore-phase1`
5. `arm-phase2`
6. `apply-phase2`
7. `rollback-phase2`
8. `finalize`

`identify` is read-only. `arm-phase1` publishes the retained phase-1 restore
executable and contract before any service is stopped. `quiesce` stops the
closed set of old supervisor and daemon resources. A durable controller
`quiesce_started` transition always precedes that call.

`arm-phase2` captures every required prior artifact and publishes a
self-contained rollback-intent contract and executable before any source,
virtualenv, environment, runtime, service definition, or supervisor mutation.
The controller journals its exact digest before recording `phase2_started`.
`rollback-phase2` is authorized by this pre-mutation intent contract. A
successful post-deploy manifest is evidence that `apply-phase2` finished; it is
never the authority required to undo an interrupted apply.

`finalize` removes only generation-owned locks, barriers, retained transient
artifacts, and release state. It is safe to replay after hub commit.

Before `open`, every node must also return a complete
`mac.fleet_prerequisite_bundle.v1`. The bundle is read-only and binds the live
endpoint identity, selected supervisor, required executables, owner-private
input files, source and runtime image identities, and loopback service
readiness. Package installation, image pulls, schema preparation, or service
repair must happen outside the cohort transaction and then be re-proved. The
same exact bundle and expectation digest are consumed by both `arm-phase2` and
`apply-phase2`; neither action may rediscover or silently repair a prerequisite.

### Route adapters

The fleet registry remains the source of the current route, not transaction
identity. Before mutation, the coordinator negotiates a secret-free resource
identity and journals it:

- bare metal or VM: verified SSH host-key fingerprint plus machine or instance
  UUID;
- Kubernetes: cluster UID plus workload object UID, with current pod UID as an
  observation rather than durable ownership;
- hub: route identity plus durable database/store UUID.

Recovery resolves the current route from `~/.mac/fleets.yaml`, reprobes the
resource identity, and compares it with the journal. A mismatch fails closed
before hub status reads or node mutation. Raw targets, credentials, and
authenticated URLs are not journal data.

## Durable state machines

Per-node states are ordered:

```text
logical_planned
  -> route_bound
  -> phase1_armed
  -> quiesce_started
  -> quiesced
  -> phase2_armed
  -> phase2_started
  -> prepared
  -> finalizing
  -> finalized
```

Abort states retain the forward state that selected recovery and record
`abort_started -> aborted` per node. Recovery walks nodes in reverse mutation
order. Its normal action is `retain_forward`: preserve the newest node state,
process barrier, dispatch hold, and immutable diagnostic bundle, then release
only the controller lock for a successor deployment. Prior-generation
compensation requires the explicit `--recovery-policy rollback` break-glass
choice and that policy is durably bound by the first node action.

Global states are:

```text
preparing
  -> hub_open
  -> hub_proved
  -> commit_intent
  -> hub_committed
  -> finalizing
  -> finalized

preparing | commit_intent-with-proved-absence
  -> aborting
  -> aborted_held
```

`commit_intent` durably binds the exact release plan before the hub request.
When the response is lost, recovery asks the hub authority for exact status. A
matching `committed` marker forces commit finalization. Exact `absent` permits
the incomplete hub epoch to close only after its proof is journaled; nodes then
remain at their newest observed state for forward repair by default.
`mismatch` or unknown transport state permits neither recovery direction nor
replay.

`hub_committed` is irreversible but is not terminal. The journal remains active
until every node finalization receipt is durable. The finalizer executable
digest is bound when phase 2 is armed, so a later controller adopts the journal
and resumes finalization with the exact installed bytes instead of its current
checkout.

`aborted_held` is terminal only when the hub staged-state abort, exact retained
hold ownership, node-local process barrier, and chosen node recovery action are
all proved. Under the default policy the action explicitly proves
`rollback_performed=false` and retains the failed generation's diagnostics. It
deliberately does not mean dispatchable.

Journal ownership binds controller nonce, boot identity, PID, and process start
time. PID liveness alone is insufficient because PID reuse after reboot can
impersonate the previous owner.

## Stuck-transaction diagnosis

A journal that is not terminal and whose owning controller is provably gone is
a *stuck* transaction. It is not self-clearing: the cohort it pinned is
re-enumerated by every subsequent deploy, so a cohort member that has since
left the fleet registry fails route resolution on every attempt.

That failure used to be reported per agent, which named the wrong subject. An
operator seeing only `authoritative SSH route resolved empty` has no way to
reach the epoch that pinned the name, and the obvious remedies — deleting the
agent, pruning the fleet registry, or passing an explicit agent list — cannot
work, because none of them touch the journal the deploy actually reads.

`diagnose` is therefore the first thing a deploy runs, before any node is
contacted:

- it reports every non-terminal epoch whose owner is dead, **by epoch id**,
  with its state, phase, age, and pinned cohort;
- it names each pinned member that is no longer an enabled agent in the frozen
  fleet registry, rather than letting it surface later as an empty route;
- it reports whether anything was ever applied to a node or committed on the
  hub, so an operator can tell whether the block is protecting real work;
- unlike `discover`, it never refuses a directory. More than one live epoch, or
  an unparseable journal, is a fact to report — it is the exact situation in
  which reporting nothing is worst.

Diagnosis is read-only. It never adopts, mutates, or removes a journal.

A dead owner by itself is not a refusal — adopting the journal and recovering
is the correct response to a crashed controller. The deploy stops before
contacting any node in exactly one case: a stuck epoch pins a name the frozen
registry no longer has. Route resolution returns nothing for that name on every
attempt, so replaying the cohort cannot succeed, and the deploy says so by
epoch instead of proving it one empty SSH route at a time.

## Journal retention

Terminal journals are evidence, not live state, and nothing used to age them
out. `reap` keeps a bounded window under three invariants:

- a non-terminal journal is never removed, at any age — it is state, and the
  stuck epoch above is exactly the case that must survive;
- an unparseable journal is reported, never deleted — it may be the only
  surviving evidence of a failure;
- the newest `--keep` terminal journals are retained regardless of age.

Auxiliary plan files (`release-plan-*`, `hub-open-plan-*`, `hub-prove-plan-*`)
are removed only when their epoch's journal is gone. `--dry-run` previews the
exact set a real pass would remove. The window defaults to 14 days and 5 kept
journals, overridable with `MAC_FLEET_COHORT_JOURNAL_RETENTION_DAYS` and
`MAC_FLEET_COHORT_JOURNAL_RETENTION_KEEP_COUNT`.

## Orphan-authority recovery

Hub HA failover (or any authority loss) can leave the controller holding a
durable journal whose hub barrier -- `open`, `prove_intent`, or `proved` -- no
longer exists at the hub: the epoch id returns exact `absent` and the pending
credential row is gone. There is no abort receipt to journal, because the epoch
that would emit one is gone. The controller must not assume the hub epoch is
durable; it retires only the matching orphan barrier after an admin-audited
proof, using the `hub-orphaned` operation.

`hub-orphaned` requires two owner-private inputs, both bound to the exact
journal:

1. an `absent` status receipt (`mac.fleet_release_epoch_status.v1`) written by
   the loopback epoch client. Only status `absent` is accepted: the client
   raises on transport failure, so absence is proven by the live hub authority
   and never inferred from a timeout. `mismatch` -- a different epoch under the
   same id -- is refused. The receipt's epoch, hub authority UUID, and identity
   digest must equal the values journalled when the epoch opened.

2. a node quiescence bundle (`mac.fleet_orphan_quiescence.v1`) proving, for the
   exact cohort and generations, that every node holds its deployment lock,
   presents a startup attestation digest, is both `idle` and `healthy`, and has
   `active_work=false`. A single non-idle, unhealthy, lock-less, busy, or
   wrong-generation node fails the whole operation closed.

On success the operation records a durable
`mac.fleet_hub_orphan_abort_evidence.v1` marker (binding the absence and
quiescence digests, not any credential), moves the hub barrier to `aborted`,
and moves the journal to `aborting`. It reconstructs no credential and never
touches the successor or any operator hold. Normal reverse-order node recovery
and `abort` then retire the matching journal exactly as for a receipt-backed
abort. Committed and finalized epochs are never orphaned -- they finalize.

`recovery` surfaces `hub_recovery.orphan_recoverable=true` on an
orphan-eligible barrier so an operator can distinguish this admin path from a
normal receipt-backed abort.

## Coordinator constraints

The coordinator advances durable participant transitions; it does not directly
edit credentials, service definitions, tunnels, policies, or holds. For every
operation it follows this sequence:

1. validate the current journal revision and participant identity;
2. persist intent before mutation;
3. invoke one bounded typed participant operation;
4. validate its exact receipt and artifact digests; and
5. compare-and-swap the resulting state.

Remote output is bounded, secret-bearing inputs are one-use, and deadlines use
monotonic clocks. Recovery never selects a helper from the current checkout; it
uses the exact retained executable recorded before mutation.

## Required fault matrix

The following are release blockers, not optional coverage:

- controller termination before and after every journal transition;
- node process termination or reboot inside phase-1 quiescence;
- node termination or reboot after phase-2 intent and before the post manifest;
- replacement or tampering of restore intent, rollback intent, helper, receipt,
  or post manifest;
- fleet-registry target swap to a different machine or workload;
- competing epoch open for one agent;
- pending credential or attestation proof followed by abort;
- commit request applied with its response lost;
- hub status timeout, exact absence, exact commit, and mismatch;
- hub authority loss that orphans an open/proved barrier (exact absence plus
  full-cohort quiescence retires only the matching journal);
- finalizer failure on any node after hub commit; and
- coordinator owner PID reuse across a reboot.

Every case must converge to one of the two objective outcomes. Tests must assert
the exact hub identities and holds as well as files and process topology.

## Delivery gates

The protocol is eligible for image publication only when:

1. the hub, node, route, and coordinator contracts pass independently;
2. the composed fault matrix passes without ambient fleet credentials;
3. every synchronized mutator appears in the classified resource inventory;
4. the complete repository contract suite passes;
5. a hostile security review finds no unjournaled mutation, credential-bearing
   journal field, unsafe private-file read, or ambiguous recovery branch; and
6. source, runtime image, and publication receipts bind one immutable commit.

The first live use remains a held five-node deployment. Only after all five
registrations, generations, principals, holds, and finalizers agree may a
single c26 canary task be released. Whole-fleet dispatch activation is a
separate explicit decision.

## Ledger DAG

- `task_f5c5950f`: typed node participant protocol
- `task_59c478d0`: atomic hub epoch authority
- `task_9c4e6102`: durable coordinator and route identity
- `task_ef76d43e`: composed integration, fault matrix, publication, deployment,
  and c26 canary; depends on the three component tasks above
