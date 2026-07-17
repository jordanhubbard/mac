# Work-Graph Assembly Control Plane

Status: implemented behind default-off activation gates; live pilot activation
awaits a reviewed digest-pinned external certifier image and trusted image-owned
harness

Ledger: `task_173ab1e7`

Recovery tags:

- `checkpoint/pre-workgraph-redesign-local-20260717`
- `checkpoint/pre-workgraph-redesign-canonical-20260717`

## Decision

MAC will operate as one logical work system over asynchronous, heterogeneous
execution cells. It will not attempt to make every machine advance on one wall
clock, and it will not allow independent workers to manufacture and publish
uncoordinated repository state.

The governing rule is:

> Plan globally, admit transactionally, release by downstream capacity,
> execute locally, integrate hierarchically, and certify the exact candidate
> before canonical publication.

The architecture combines two proven control patterns:

1. A parallel dataflow runtime: immutable/versioned logical graphs, dependency
   readiness, attempt fencing, private outputs, selective fan-in, and explicit
   commit.
2. A high-mix flexible manufacturing cell: central work release, heterogeneous
   local execution, limited work in process, quality at source, inspection,
   and bounded rework.

The task ledger remains the canonical execution store. A new `WorkPackage`
aggregate owns the plan and its relationship to task, lease, evidence, review,
assembly, and publication records. Task metadata may project this relationship
for worker context, but metadata is not authoritative for admission or fencing.

## Current implementation boundary

The repository now contains the durable package/plan/epoch/node schema, pure
plan compiler, transactional admission, managed planner/API/IDE surface,
capacity- and conflict-aware allocation, immutable attempt-output attribution,
controller verification, integration batches, external certification jobs,
crash-resumable compare-and-swap landing, publication finalization, and exact
protected-ref retirement receipts. SQLite and PostgreSQL use the same logical
contract.

The controller loop remains deliberately disabled by default. Package
activation and every worker claim fail closed unless the same registered
repository row proves an enabled, independently pinned certification contract,
an exact secret-free canonical landing endpoint, and enabled downstream
pipeline/landing capacity. Controller replicas use distinct lease-owner
identities; a process restart or a second replica must reacquire authority
rather than inheriting another process's name.

This is deployable control-plane machinery, not yet a claim that the production
fleet is running the new assembly line. Live activation requires the separately
reviewed certifier artifact and canary sequence in
`docs/work-package-pipeline-activation.md`. Until then, existing non-package
tasks continue through the legacy path and newly accepted packages remain held.

## Why this replaces both extremes

A physical single-system image or bulk-synchronous fleet would amplify slow
nodes, WAN partitions, pod preemption, and machine-specific failures into
global stalls. Completely independent workers instead amplify overlap, stale
bases, duplicate work, integration queues, and conflicting publication.

MAC therefore uses logical rather than physical synchronization:

- every admitted plan has an immutable version and exact base SHA;
- every execution wave belongs to a monotonically increasing epoch;
- every mutation is authorized by an exact lease/fence, not merely an agent ID;
- workers run at their natural speeds;
- barriers exist only at real dependency joins, integration batches,
  certification, and publication;
- a replan creates a new generation and fences stale work instead of silently
  editing the active graph.

## What the neighboring disciplines actually contribute

This is a deliberate hybrid, not a metaphor applied after the fact.

| Source pattern | Useful translation into MAC | Literal behavior MAC rejects |
| --- | --- | --- |
| Bulk-synchronous parallel supersteps | Epochs and explicit fan-in barriers give a coherent logical generation | A fleet-wide wall-clock barrier would let the slowest WAN node or partition stop unrelated work |
| Dataflow/DAG execution | Versioned vertices, explicit dependencies, immutable edge outputs, retryable attempts, and selective joins | Treating arbitrary repository mutations as side-effect-free data records |
| CONWIP/pull production | Release new mutations only when bounded downstream product capacity exists | Keeping every worker busy while candidate and integration queues grow without bound |
| Reconfigurable manufacturing cells | Match a stable operation contract to heterogeneous capability cells and change capacity without changing product identity | Pretending unlike Macs, Linux hosts, ARM nodes, and pods are interchangeable processors |
| Jidoka/Andon | Detect abnormality at the producing station, preserve evidence, and stop only the affected scope | Letting a known-bad artifact continue downstream so an end-of-line test can rediscover it |

The parallel-computing precedents are Valiant's
[BSP bridging model](https://doi.org/10.1145/79173.79181), Microsoft's
[Dryad DAG engine](https://www.microsoft.com/en-us/research/wp-content/uploads/2007/03/eurosys07.pdf),
and Google's
[Dataflow model](https://research.google.com/pubs/archive/43864.pdf).
The manufacturing precedents are Spearman, Woodruff, and Hopp's
[CONWIP pull system](https://doi.org/10.1080/00207549008942761), Koren and
Shpitalni's
[reconfigurable manufacturing design](https://public.websites.umich.edu/~ykoren/uploads/Design_of_Reconfigurable_Manufacturing_Systems.pdf),
and Toyota's description of
[Jidoka and Just-in-Time](https://global.toyota/en/company/vision-and-philosophy/production-system/).

The result is better than either extreme for MAC's workload: it preserves local
asynchrony and heterogeneous specialization while giving the fleet one durable
product identity and a small number of exact synchronization points.  It costs
more control-plane state and adds latency for tiny changes, which is why the
single-node fast lane remains.  The first high-value adoptions are therefore
versioned DAGs, immutable attempt outputs, pull-based WIP limits, station-local
Andon, exact assembly/certification, and compare-and-swap landing—not a global
clock or a physical single-system image.

## Authority model

Intelligent agents propose and interpret. Deterministic controllers admit and
actuate.

| Role | May do | Must not do |
| --- | --- | --- |
| Work-plan agent | Propose nodes, dependencies, scopes, estimates, and acceptance contracts | Materialize tasks, grant leases, or publish |
| Plan compiler | Normalize and validate a complete plan deterministically | Invent missing semantic requirements |
| Allocation advisor | Score legal ready waves over current fleet state | Mutate the ledger directly |
| Admission controller | Atomically reserve capacity and bind an exact task/agent/lease/epoch | Override hard eligibility or capacity invariants |
| Worker cell | Execute one fenced node and submit immutable evidence/artifacts | Treat its branch as the assembled product |
| Integration agent | Assemble exact inputs, diagnose semantic conflicts, and propose bounded rework | Publish an unverified or differently assembled tree |
| Certifier | Run the declared gates against an exact candidate SHA | Certify a moving branch name |
| Landing controller | Compare-and-swap one certified candidate onto the canonical ref | Rebuild or modify the certified candidate during landing |

There may be multiple planning or scheduling advisors. Their proposals are
optimistic reads against a ledger revision. One deterministic transaction
validates the revision and commits the decision. This preserves specialization
without creating competing control-plane authorities.

## Logical pipeline

```text
objective / product contract
          |
          v
  work-plan proposal
          |
          v
 deterministic compilation
          |
          v
 versioned WorkPackage + epoch
          |
          v
 capacity/conflict-aware ready wave
          |
     +----+----+----------------+
     v         v                v
 worker A   worker B         worker C
 private    private          private
 attempt    attempt          attempt
     |         |                |
     +----+----+----------------+
          v
 hierarchical integration batches
          |
          v
 exact candidate SHA + certification
          |
          v
 repository landing lease + CAS push
          |
          v
 canonical proof / completion
```

## Durable aggregates

### Work package

A work package is the stable identity of one product objective. It owns:

- project and repository identity;
- root/integration task;
- current accepted plan version;
- current execution epoch;
- state and scoped hold/Andon condition;
- WIP and rework policy;
- current canonical base ref and SHA;
- created-by provenance and timestamps.

The package survives replanning. Plan versions and epochs do not.

### Plan version

An accepted plan version is immutable. It contains:

- a canonical graph document and digest;
- parent version and replan reason;
- exact base ref/SHA used for compilation;
- graph nodes and edges;
- compiler version and policy version;
- acceptance decision and actor.

Changing any node, dependency, effect, output, acceptance rule, or base creates
a new version. No in-place mutation is permitted after acceptance.

### Node contract

Every executable node declares:

```yaml
node_key: stable-within-plan
kind: analysis | mutation | integration | certification
depends_on: []
required_capabilities: []
inputs: []
expected_outputs: []
effects:
  reads: []
  writes: []
  exclusive: []
  external: []
verification:
  profile: repository-default
  commands: []
estimates:
  duration_seconds: null
  cost_units: null
  confidence: low | medium | high
rework:
  max_cycles: 1
metadata: {}
```

Paths must be repository-relative and normalized. `..`, absolute paths,
credential-bearing URLs, and secrets are rejected. External effects require an
idempotency or exclusivity declaration. Empty scope declarations are allowed
only with low confidence and conservative scheduling.

Declarations do not fence an already partitioned worker from completing an
external action after its lease expires. Retriable external effects therefore
flow through controller-owned transactional outbox/effectors that use the
target system's idempotency or fencing primitive. An effect that cannot be
fenced or made idempotent is single-attempt and routes to manual Andon after an
ambiguous outcome; MAC must not dispatch a second worker and hope duplication
did not occur.

The current managed executor has no such external effector. Admission and the
claim gate therefore reject every executable node with an `external` effect.
`external_contract.idempotency_key` remains a future effector contract; merely
declaring it never authorizes a worker to perform the action.

### Current flat mutation-wave safety envelope

Every worker mutation attempt is currently pinned to the immutable planning
base SHA of its package epoch. The controller does not yet have a pre-claim
composition station that can assemble predecessor commits, publish an exact
protected base ref, attest its lineage, and bind the downstream assignment to
that new base in one fenced protocol. The executable graph therefore fails
closed unless all repository mutations form one flat wave:

- no mutation may have a mutation ancestor, directly or through another node;
- no mutation may have an integration ancestor;
- every mutation in the wave starts from the same epoch planning base;
- accepted mutation candidates flow downstream into explicit integration
  fan-in;
- the pure compiler can describe nested integration groups, but the current
  integration runtime release-blocks nested integration inputs until it has an
  append-only exact provenance receipt for the predecessor batch;
- after repository integration begins, only controller-owned certification
  work may remain on the currently executable repository lineage.

Analysis dependencies may precede the wave, but their outputs are planning or
evidence artifacts, not an implicit Git base. A planner that needs `mutation B`
to edit the result of `mutation A` must currently combine them into one mutation
node or end the first package/epoch, land it, and plan the next package from the
new exact canonical SHA. It must not encode `A -> B` and assume task dependency
ordering also propagates repository state.

This is a deliberate pilot restriction, not the target limit of the DAG model.
True composed mutation stages require a controller-owned base-composition
receipt containing the exact predecessor candidates and trees, deterministic
assembly strategy, protected composed-base ref/SHA, lineage digest, monotonic
fence, and stale-generation checks. Only then may assignment use that receipt's
SHA instead of the epoch planning SHA. Until that full boundary exists, the
compiler, activation transaction, claim transaction, integration station, and
landing controller all reject the unsafe topology, including plans admitted by
an older controller version.

### Task links

Tasks remain the unit of executable lifecycle. A control-plane-owned link maps
exactly one materialized task to package, plan version, epoch, and node key.
Worker-visible task metadata mirrors this contract but cannot create or modify
the authoritative link.

Dependencies of a linked task are compiler-owned. Generic task updates must
not mutate them. A replan supersedes nodes through the work-package service.

### Assignment audit and fence

An assignment binds:

```text
package + plan version + epoch + node + task + lease + agent
```

The lease ID is the minimum write fence. Worker-originated start, evidence,
review submission, completion, renewal, and release calls must present the
expected lease ID. Agent identity alone is insufficient because the same agent
may receive a replacement lease after expiry. Package-linked calls also verify
the current plan version and epoch.

Stale attempts may remain readable evidence, but they cannot advance current
state or enter an integration batch.

Lease and landing expiry use authoritative control-plane/database time. Worker
clocks are telemetry only. An assignment whose expiry has passed cannot be
renewed back to life before the sweeper notices it; it must be re-admitted under
a new lease and fence.

### Execution capacity and product WIP

Execution capacity and product inventory are different resources and must not
share one lifetime.

Execution capacity is represented by durable slots bound to an active lease:

- per-agent concurrency;
- scarce machine/tool capacity;
- exclusive execution resources.

A claim transaction reserves the task lease and every required execution slot
together, or reserves nothing. When the exact lease becomes inactive, its
execution slots are reclaimable. This makes crash recovery idempotent without
leaking machine capacity.

Product WIP is represented by separate durable tokens:

- package mutation inventory;
- repository mutation inventory;
- repository integration inventory;
- certification/landing inventory.

A mutation acquires its WIP token when work is admitted. Worker completion does
not release it. The token is atomically transferred to the next downstream
stage, or returned only when the product is accepted, explicitly scrapped, or
quarantined according to policy. This distinction prevents a dead worker from
freeing product capacity and causing unbounded branch inventory.

The compiler and admission controller must also prove flow liveness. A fan-in
of width three cannot hold two mutation tokens until all three outputs exist
when the WIP limit is two. Plans therefore use bounded candidate-output buffers
and atomic stage-token transfer, or reserve sufficient downstream/fan-in
capacity before releasing the wave. No accepted capacity policy may make a
declared graph structurally unable to complete.

### Node progress and attempt outputs

Package-node progress is distinct from ordinary task completion. In particular,
a repository child may reach `candidate_accepted` once its fenced output and
verification evidence are valid for assembly. It does not need to publish or
become canonically `COMPLETED` before dependent package nodes can proceed.

An authoritative attempt-output link binds every selectable evidence/artifact
to the exact assignment and lease that produced it. Evidence JSON or task
metadata cannot claim a different attempt. Package-aware readiness consumes
accepted node outputs; legacy non-package readiness continues to consume
completed task dependencies.

The worker may create only the attribution link for its exact assigned attempt:
task, lease, attempt number, protected attempt ref, immutable planning base, and
claimed head. At the same atomic append, the hub derives a canonical
`mac.work_package.attempt_artifact_manifest.v1` digest from the exact
ref/base/head, declared-effects digest, protocol declaration, and normalized
durable-artifact identities (name, type, media type, encoding, size, content
SHA-256, and truncation state). Artifact order is normalized. Evidence and
artifact database IDs, timestamps, summaries, arbitrary metadata, bytes, and
source/content URIs are excluded, so credentials and controller-local
locations cannot enter the content address. The resulting `sha256:` value is
stored in the append-only attribution row on its initial insert; it is never
backfilled or mutated later.

The worker cannot mark that link verified or later substitute a mutable branch.
After independent repository inspection, the controller appends a separate
immutable verification receipt containing the observed ref/head/tree, artifact
and effects digests, changed paths, verifier identity, and receipt digest. The
attribution row is never promoted into authority by mutation. Acceptance and
assembly join the attribution to that append-only receipt and revalidate the
full identity. A failed verification leaves the attribution unselectable and
records an Andon finding instead of trusting worker-supplied metadata.

Repository output is addressed by an immutable attempt/lease-specific ref or
content-addressed bundle, not a mutable task branch name. That artifact remains
protected from ref reconciliation until the package and retention policy make
it disposable.

The hub is the only authority that may retire these protected refs. Candidate
and attempt refs are discovered from controller-owned records, not inferred
from a namespace scan. Retirement requires a terminal lifecycle, a grace
period, a known exact SHA, a force-with-lease delete against that SHA, remote
read-back, and append-only intent/attempt/receipt records. Unknown `refs/mac/*`
and recorded refs whose SHA was never independently observed remain untouched
and surface as audit debt instead of being guessed away.

At candidate acceptance, the controller computes the actual changed
files/symbols from the attempt base to the immutable output and compares them
with the declared write contract. Undeclared expansion is a scoped Andon and
replan request; it is never silently treated as a successful prediction.

### Integration batch

An integration batch selects immutable inputs:

- exact task, assignment, and lease attempt;
- evidence IDs and artifact digests;
- exact commit SHAs;
- deterministic input order;
- target base SHA;
- component/integration scope.

Batch membership is immutable after assembly begins. A changed input creates a
new batch. Integration may reject, split, accept, or route bounded rework; it
cannot silently substitute a newer branch head.

Every batch claim carries a monotonically increasing fence token in addition to
owner and expiry. A process whose lease expired cannot update or land the batch
after another process reacquires it, even if the owner identifier is reused.

### Certification and landing

A certification names one exact candidate commit and records:

- candidate ref and SHA;
- base and ordered input SHAs;
- test policy and commands;
- CodeGraph audit when applicable;
- output evidence and checksums;
- certifier identity;
- pass/fail result and validity conditions.

The repository landing controller serializes candidates by canonical
repository and branch using a database-backed lease. It then performs a
compare-and-swap push from the certified base to the certified candidate. If
the canonical ref moved, that candidate cannot land under its old receipt: the
same immutable component outputs are reassembled onto the new landing base and
the resulting candidate is retested. Landing must never create an untested
merge commit after certification.

Remote canonical verification is an actual remote-ref read-back, not an
assumption following a successful local command.

Git push and the ledger cannot be one atomic transaction. Landing therefore
uses a crash-resumable protocol: persist landing intent and fence, perform the
remote compare-and-swap, read back the remote ref, then finalize the durable
receipt. Recovery classifies `remote == candidate` as already landed and
finalizes idempotently; it must not synthesize a different merge after an
ambiguous response.

Certification executes untrusted candidate code in an isolated runtime that
has no canonical-push credential. Its test policy is pinned from the trusted
base/repository contract, so a candidate cannot replace its gate with a no-op.
The landing controller has push authority but never executes candidate code.

## Deterministic plan compilation

Compilation is pure and repeatable for the same inputs and compiler version.
It rejects:

- missing or duplicate node keys;
- cycles;
- dependencies on unknown or later-invalid nodes;
- nodes with no meaningful output or acceptance contract;
- malformed or unsafe effects;
- unbounded fan-out or rework;
- incompatible external effects in one wave;
- missing integration/fan-in for multiple mutation leaves;
- mutation nodes with mutation or integration ancestors while exact composed
  predecessor bases are unsupported;
- policy limits exceeded by the graph.

The compiler computes:

- canonical graph digest;
- topological order and levels;
- critical-path rank using declared estimates;
- conflict domains and conservative conflict confidence;
- default integration groups;
- required capacity scopes;
- package-relative materialization keys from which admission allocates unique
  durable task IDs.

The pure compiler validates the repository/base reference shape but cannot
prove that an external repository, ref, or commit currently exists. The
transactional admission controller resolves the registered repository and
exact base generation immediately before materialization and rejects any
compile/admit drift. A compiled graph is a proposal until that admission record
exists.

CodeGraph may enrich predicted read/write blast radius. Its result is evidence,
not an excuse to omit explicit effects. Hidden semantic coupling remains
possible, so low-confidence scopes schedule conservatively and receive broader
integration verification.

Effect comparison uses a repository-derived canonical resource namespace for
the exact base tree: filesystem case behavior, Unicode normalization, and
symlink aliases are resolved before conflict decisions. When those semantics
are unknown, admission uses conservative aliases rather than assuming Linux
case sensitivity on a mixed macOS/Linux fleet.

## Readiness and scheduling

`OPEN` is not sufficient proof of readiness. Every claim transaction rechecks:

1. task state, owner, and hold state;
2. every durable task dependency is completed/accepted as required;
3. linked package, plan version, and epoch are current and runnable;
4. node predecessors are accepted into the required stage;
5. project/repository dispatch is active;
6. worker trust, tenant, runtime, command, hardware, role, and capability
   eligibility;
7. worker capacity under a serialized agent-row decision;
8. package/repository WIP capacity;
9. no hard effect/exclusive conflict with admitted work;
10. the task and assignment revision still match the advisor's proposal.

Downstream pull is part of that authoritative claim transaction, not merely an
earlier scheduler observation. The transaction revalidates the locked
registered repository row, certification contract, canonical endpoint, and
enabled pipeline/landing configuration immediately before reserving the lease
and WIP. If downstream closes between scheduling and claim, the claim loses the
race and no attempt is consumed.

The allocation advisor scores only legal candidates. Initial scoring is:

```text
critical-path rank
+ task priority and age
+ recent repository-access success
+ capability confidence
+ repository/cache/data locality
- predicted duration and transfer cost
- conflict uncertainty
- integration-queue pressure
- recent classified failure on the same route
```

The first rollout is advisory/observable. Hard serialization initially applies
only to explicit `exclusive` and externally mutating effects. Predicted
file/symbol conflicts rank work into different waves until precision is
measured.

Work stealing remains useful only inside a scheduler-authorized,
conflict-compatible ready set. Workers do not browse the entire ledger and
independently decide that similarly shaped work is safe.

## Replanning and rework

Replanning is a state transition, not metadata editing:

1. record the trigger and affected nodes;
2. compile plan version `N+1` against an explicit base;
3. increment the package epoch;
4. release/fence assignments that are no longer valid;
5. calculate an explicit carry-forward map for completed immutable artifacts
   whose node contract digest and complete input lineage remain valid;
6. supersede unstarted/invalidated tasks;
7. materialize only the changed downstream cone;
8. resume admission under the new WIP policy.

Carry-forward is an authoritative record from an old
`(plan_version, epoch, node, assignment, output_digest)` to the new node
generation. A package-wide epoch change fences old mutation authority but does
not erase provenance. Without a verified carry-forward edge, an old-epoch
artifact cannot satisfy a new-epoch node or enter its integration batch.

Three bases are recorded separately:

- the plan/node contract generation;
- the base used by one execution attempt;
- the current assembly/landing base.

Ordinary canonical movement changes the last of these, not automatically the
first two. Immutable outputs are reapplied to the current assembly base and are
invalidated only by a real conflict, changed input/contract digest, or failed
verification. This prevents concurrent packages from repeatedly invalidating
each other without semantic cause.

Every node and package has a bounded rework budget. Exhaustion produces a
durable quarantine or human-review condition, not recursively generated repair
families.

## Quality-at-source and scoped Andon

An Andon condition has a typed scope and owner:

- assignment: stale source, missing toolchain, invalid credential route;
- node: failed invariant or exhausted rework;
- package: invalid plan or systemic interaction failure;
- repository: integration backlog, landing race, canonical divergence;
- fleet route: repeated classified authentication/runtime failure.

The affected scope stops accepting new work while unrelated packages and
repositories continue. Evidence is preserved before the stop. Recovery is an
explicit superseding event, not repeated blind retry.

## Fast lane

The target architecture keeps a managed single-node fast lane because planner
and assembly overhead is wasteful for a genuinely atomic change. It is legal
when all are true:

- one coherent executor node;
- no external side effect other than its fenced repository branch;
- no declared shared exclusive resource;
- repository WIP capacity is available;
- ordinary review/certification/landing still applies.

That managed fast lane omits multi-node planning and component-integration
overhead. It still uses leases, exact-candidate certification, compare-and-swap
landing, and completion proof.

Existing unlinked tasks are a compatibility path, not that fully migrated fast
lane. They retain their current executor pre-push tests, CodeGraph audit,
review, and publication rules, but they do not automatically acquire the new
external exact-candidate certifier or work-package landing receipt. Describing
the legacy path as if it already had those properties would be false. Migration
is complete only when single-node package admission is the ordinary route or
equivalent exact-candidate guarantees are added to the legacy path.

## Compatibility and migration

1. Existing tasks without a package link continue through the legacy
   compatibility path described above.
2. Existing cooperative families can be adopted into a package by recording an
   immutable inferred version; inference never trusts worker-authored metadata
   without verifying the task relationships.
3. Fleet IDE managed workflow-plan acceptance creates a held work package and
   materializes its task graph instead of creating unrelated tasks. The frozen
   legacy dashboard remains unchanged.
4. `add_child_tasks` becomes an atomic work-package admission path. Existing
   callers retain their response shape.
5. Sequential `WorkflowRuntime` remains for conditional role workflows. It is
   not repurposed as the parallel graph runtime because its definitions permit
   cycles and its run has one current node/task.
6. Existing evidence, reviews, publications, and task history remain canonical;
   work-package records link to them rather than introducing parallel artifact
   stores.
7. Generic task dependency mutation is refused after package linkage.
8. Package-linked tasks require an explicit `work_package_v1` runtime
   capability and package-aware dispatcher. Admission remains disabled during
   a rolling upgrade until legacy hubs/workers are drained or source-converged;
   an old dispatcher must not be able to claim a new package node as an ordinary
   `OPEN` task.

## Historical failures converted into invariants

The redesign exists to eliminate observed failure classes, not just to add a
new abstraction:

| Observed failure | New invariant |
| --- | --- |
| Concurrent work diverges from moving `main` | Version and epoch name an exact base SHA; stale work is fenced |
| Publisher validates one tree and pushes another | Certification names the exact pushed candidate SHA |
| Concurrent publishers race after identical checks | Repository landing lease plus compare-and-swap push |
| Workers duplicate/overlap changes | Declared effects, conflict-compatible waves, and repository WIP |
| Task/repair explosions | Admission limits, integration-capacity pull, bounded rework |
| Same agent reacquires a task after expiry | Every worker write presents the exact lease fence |
| Capacity-one worker receives two tasks | Capacity is rechecked and reserved in the claim transaction |
| `OPEN` task with unfinished dependencies is claimed | Claim transaction rechecks durable dependencies |
| Cooperative children land separately before parent assembly | Child output is an immutable candidate input; package completion occurs after assembled certification |
| Moving branch is hub-tested without exact SHA assertion | Clone/check-out and test require `HEAD == candidate_sha` |
| Authentication failures are blindly retried | Typed scoped Andon plus fleet operational learning |
| Integration becomes one end-of-line bottleneck | Hierarchical batches and downstream capacity tokens |

## Observability and optimization

Record processing time and queue time separately for:

- planning;
- admission;
- ready but capacity-held;
- execution;
- component integration;
- certification;
- landing;
- rework/quarantine.

Primary outcome metrics are:

- time from objective to canonical commit;
- critical-path time and parallel efficiency;
- canonical throughput;
- aggregate worker/model cost;
- stale/discarded work;
- conflict prediction precision and recall;
- integration failures and candidate invalidations;
- rework cycles;
- delayed defects after publication.

The scientific optimizer may experiment with scheduling weights, WIP limits,
batch size, planner strength, and review policy. It may not auto-relax safety,
fencing, certification, or canonical-landing invariants.

## Rollout

### Stage 0: safety and observation (implemented in the default-off control plane)

- close dependency, worker-capacity, and lease ABA races;
- add durable package schema and pure compiler;
- record inferred effects and conflict predictions without blocking;
- instrument stage queues and canonical lead time.

### Stage 1: managed package pilot (code complete; deployment activation pending)

- one `mac` package at a time;
- three to five coherent mutation nodes;
- package mutation WIP of two;
- one repository integration slot;
- existing cooperative child execution;
- exact-candidate certification and serialized landing;
- manual comparison with the current fast lane.

The remaining entry condition is external rather than a hidden feature flag:
publish and review the digest-pinned certifier image plus its image-owned test
harness, update the repository contract atomically, deploy to a durable hub,
and pass both successful and failing-certification canaries before releasing
real package work.

### Stage 2: default for parallel mutation (future rollout)

- dashboard and executor planning materialize packages;
- conflict-compatible ready-wave allocation;
- plan epochs and controlled replanning;
- hierarchical integration batches;
- per-project WIP configuration.

### Stage 3: adaptive scheduling (future optimization)

- learned duration/cost estimates;
- measured conflict-confidence thresholds;
- optimizer experiments over flow parameters;
- regional/cell locality and transfer-aware placement;
- speculative duplication only for side-effect-free analysis/test nodes.

## Completion criteria

The redesign is complete only when current-state evidence proves:

1. managed plans, versions, epochs, nodes, and task links are durable on SQLite
   and Postgres;
2. plan plus task materialization is atomic or crash-resumably transactional
   with deterministic reconciliation;
3. every claim rechecks dependencies, worker capacity, package epoch, effects,
   and WIP in the authoritative transaction;
4. stale leases and stale epochs cannot mutate current work;
5. execution capacity and product WIP have distinct, crash-safe lifetimes and
   every accepted graph/capacity policy has a demonstrated progress path;
6. actual attempt scope is checked against declared effects and expansion
   cannot silently enter assembly;
7. component outputs use immutable attempt artifacts and remain private
   candidates until assembly;
8. certification tests the exact candidate that landing attempts to publish,
   under an independently pinned policy and without landing credentials;
9. landing is serialized across hub replicas, uses compare-and-swap, and
   recovers idempotently from a crash after remote push;
10. canonical remote read-back matches the recorded receipt;
11. replanning invalidates only the affected cone and preserves valid immutable
   work;
12. bounded rework and scoped Andon prevent recursive failure storms;
13. mixed-version rollout cannot let a legacy hub/worker claim package work;
14. existing single-task workflows retain a functional fast lane;
15. APIs, CLI, IDE projections, operator documentation, migrations, and
    observability expose the new truth;
16. CodeGraph audit and the full repository contract suite pass against the
    final tree;
17. the implementation is pushed and the canonical task ledger is reconciled.
