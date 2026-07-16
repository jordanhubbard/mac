# Work-Graph Assembly Control Plane

Status: accepted architecture, implementation in progress

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

### Capacity pools

Capacity is represented by durable slots, not a count performed before a
transaction. Initial scopes are:

- package mutation WIP;
- repository mutation WIP;
- repository integration WIP;
- scarce/exclusive resource WIP.

A claim transaction reserves every required slot and the task lease together,
or reserves nothing. A slot whose associated lease is no longer active is
reclaimable. This makes crash recovery idempotent and avoids leaked in-memory
counters.

Mutation capacity returns only when the assignment has been accepted into the
appropriate downstream stage, explicitly scrapped, or quarantined according to
policy. A worker process exit by itself is not product completion.

### Integration batch

An integration batch selects immutable inputs:

- exact task and lease attempt;
- evidence IDs and artifact digests;
- exact commit SHAs;
- deterministic input order;
- target base SHA;
- component/integration scope.

Batch membership is immutable after assembly begins. A changed input creates a
new batch. Integration may reject, split, accept, or route bounded rework; it
cannot silently substitute a newer branch head.

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
the canonical ref moved, the certification is invalid for landing: a new
candidate is assembled and retested. Landing must never create an untested
merge commit after certification.

Remote canonical verification is an actual remote-ref read-back, not an
assumption following a successful local command.

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
- unknown repository/base generation;
- policy limits exceeded by the graph.

The compiler computes:

- canonical graph digest;
- topological order and levels;
- critical-path rank using declared estimates;
- conflict domains and conservative conflict confidence;
- default integration groups;
- required capacity scopes;
- a materialization map from node keys to durable task IDs.

CodeGraph may enrich predicted read/write blast radius. Its result is evidence,
not an excuse to omit explicit effects. Hidden semantic coupling remains
possible, so low-confidence scopes schedule conservatively and receive broader
integration verification.

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
5. preserve completed immutable artifacts whose contracts and inputs remain
   valid;
6. supersede unstarted/invalidated tasks;
7. materialize only the changed downstream cone;
8. resume admission under the new WIP policy.

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

The managed graph is mandatory for multi-node mutation plans and may be chosen
for any task. A small task may use the legacy-compatible fast lane when all are
true:

- one coherent executor node;
- no external side effect other than its fenced repository branch;
- no declared shared exclusive resource;
- repository WIP capacity is available;
- ordinary review/certification/landing still applies.

The fast lane omits planner and component-integration overhead. It does not
bypass leases, exact-candidate certification, canonical landing, or completion
proof.

## Compatibility and migration

1. Existing tasks without a package link continue through the fast lane.
2. Existing cooperative families can be adopted into a package by recording an
   immutable inferred version; inference never trusts worker-authored metadata
   without verifying the task relationships.
3. Dashboard workflow-plan acceptance creates a work package and materializes
   its task graph instead of creating unrelated tasks.
4. `add_child_tasks` becomes an atomic work-package admission path. Existing
   callers retain their response shape.
5. Sequential `WorkflowRuntime` remains for conditional role workflows. It is
   not repurposed as the parallel graph runtime because its definitions permit
   cycles and its run has one current node/task.
6. Existing evidence, reviews, publications, and task history remain canonical;
   work-package records link to them rather than introducing parallel artifact
   stores.
7. Generic task dependency mutation is refused after package linkage.

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

### Stage 0: safety and observation

- close dependency, worker-capacity, and lease ABA races;
- add durable package schema and pure compiler;
- record inferred effects and conflict predictions without blocking;
- instrument stage queues and canonical lead time.

### Stage 1: managed package pilot

- one `mac` package at a time;
- three to five coherent mutation nodes;
- package mutation WIP of two;
- one repository integration slot;
- existing cooperative child execution;
- exact-candidate certification and serialized landing;
- manual comparison with the current fast lane.

### Stage 2: default for parallel mutation

- dashboard and executor planning materialize packages;
- conflict-compatible ready-wave allocation;
- plan epochs and controlled replanning;
- hierarchical integration batches;
- per-project WIP configuration.

### Stage 3: adaptive scheduling

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
5. component outputs remain private candidates until assembly;
6. certification tests the exact candidate that landing attempts to publish;
7. landing is serialized across hub replicas and uses compare-and-swap;
8. canonical remote read-back matches the recorded proof;
9. replanning invalidates only the affected cone and preserves valid immutable
   work;
10. bounded rework and scoped Andon prevent recursive failure storms;
11. existing single-task workflows retain a functional fast lane;
12. APIs, CLI, IDE projections, operator documentation, migrations, and
    observability expose the new truth;
13. CodeGraph audit and the full repository contract suite pass against the
    final tree;
14. the implementation is pushed and the canonical task ledger is reconciled.
