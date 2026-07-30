# ADR 0013 - One authoritative hub allocator

- Status: **Accepted; immediate hard cutover**
- Date: 2026-07-29
- Decision owner: MAC fleet owner

## Context

MAC's dispatcher evolved into several overlapping eligibility systems:

- operator-facing `ready` and `why-unclaimed` queries;
- hub push dispatch;
- worker pull dispatch;
- worker-local project, metadata, and canary filters;
- work-package-specific ordering and admission;
- a transactional claim path that repeated a different subset of the checks.

These paths could disagree. In the incident that led to this decision, two
tasks were reported ready with idle compatible workers, but their stored
dependency IDs were abbreviated. The diagnostic path resolved the prefixes
and reported the tasks as eligible. The transactional claim used exact IDs,
rejected them, swallowed the rejection in poll-level debug telemetry, and
returned no assignment. Workers remained idle while the control plane
continued to describe the queue as runnable.

The problem is architectural rather than one missing special case. An
advisory readiness calculation cannot be authoritative if another component
may reject its answer using different rules. Worker-local filters also make
fleet-wide allocation and diagnostics impossible: the hub cannot maximize
useful work when part of the scheduling policy exists only inside each polling
process.

MAC is a fix-forward work system. Dispatch should maximize useful matching
between runnable work and available capacity. Safety and trust boundaries must
remain explicit, but preferences and repairable environment conditions must
not silently turn into reasons to strand work.

## Decision

MAC adopts one global, hub-owned, authoritative allocator. The allocator's
successful output is an atomic task-and-agent lease, not an advisory candidate
list.

This is a hard cutover. The legacy push dispatcher, worker-side candidate
scanner, worker-local hard filters, and duplicated readiness reconstruction
are removed rather than retained as compatibility paths. Existing workers do
not constrain the cutover; they may be rebuilt or restarted against the new
protocol.

The primary invariant is:

> If MAC reports a task-agent pair as assignable, the same allocation round
> either commits its lease or retains a concrete transactional race or defect
> explaining why it did not.

If runnable work and compatible free capacity coexist, the allocator must
create assignments within one scheduling round. A failed claim for one pair
does not terminate the round or prevent other tasks from being assigned.

## Canonical task graph

Dependencies are normalized before they enter scheduling state.

- The CLI and API may accept a unique task-ID prefix for operator convenience.
- The write boundary resolves that prefix exactly once.
- Only full canonical task IDs are persisted.
- Normalized dependency edges are foreign-keyed task-to-task records.
- Missing, ambiguous, cyclic, and self-referential dependencies are rejected
  at the write or migration boundary.
- Existing abbreviated dependencies are migrated to full IDs when uniquely
  resolvable. An ambiguous or missing reference becomes one explicit repair
  finding for that task; it is not reinterpreted differently by every worker.

The allocator consumes dependency satisfaction from the normalized graph in
the same database snapshot used for allocation. Blocked tasks are excluded
before task-agent pair evaluation, avoiding a worker-sized scan for work that
cannot run.

## One allocation authority

The hub owns the complete scheduling inputs:

- normalized runnable task state;
- registered and active project state;
- current task leases and attempt budgets;
- agent heartbeat freshness and slot capacity;
- machine trust and tenant authorization;
- required physical capabilities and resources;
- explicit holds and explicit target-agent restrictions.

One deterministic `evaluate_task` / `evaluate_pair` contract classifies those
inputs. The batch allocator orders runnable tasks by priority and age, then
chooses the least-loaded compatible worker. Repository locality, prior
success, and similar affinity may break ties but cannot override hard
constraints.

The allocator is work-conserving: it continues matching until it exhausts
runnable work, compatible free capacity, or the explicit round limit. It does
not invoke a model to make ordinary scheduling decisions.

`task ready`, `task why-unclaimed`, allocation previews, the Fleet IDE, elastic
capacity signals, and task-throughput diagnostics consume the allocator's
authoritative read model. They do not reconstruct eligibility independently.

## Hard constraints, affinities, and repair observations

Only the following classes are hard allocation constraints:

- the task is open, released, unleased, and within its attempt budget;
- all canonical dependencies required by its join policy are satisfied;
- its project is registered and active;
- the agent has a current healthy heartbeat and an available slot;
- machine trust, tenant authorization, and confinement boundaries match;
- required platform, architecture, hardware, and capabilities match;
- an explicit task-to-agent restriction, when present, matches.

The following are soft affinities:

- project lists formerly advertised as `allowed_projects`;
- repository locality and warm caches;
- prior success on the project;
- load balancing and project fairness;
- executor/reviewer diversity;
- preferred model or tool strength above a declared minimum.

The following are repair observations, not permanent scheduling prohibitions:

- a missing or stale checkout;
- dirty managed source;
- an installable command or runtime layer that is absent;
- an expired coding-route probe;
- a stale deployment generation;
- credentials missing from one approved source when another may be used.

The allocator may choose another compatible worker or enqueue bounded
preparation. It must retain the observation and continue the round. A
repairable condition must not make a task disappear from the scheduler.

## Atomic claim boundary

Allocator v2 commits through one narrow claim primitive. In one transaction it
rechecks the locked hard state, reserves exact agent capacity, creates the
fenced lease, and transitions the task to claimed.

The claim primitive may reject a proposal only because:

- another transaction changed the task, dependency, hold, lease, or capacity;
- a hard trust, tenant, target, or capability fact changed;
- persistence failed.

It must not call the legacy claim path if that path adds source cleanliness,
command presence, coding-route, work-package, canary, project-preference, or
other hidden predicates after selection.

Concurrent allocator replicas use database locking appropriate to the
production store, including row locking and `SKIP LOCKED` where supported.
There may never be two live leases for one work item or more leases for an
agent than its registered capacity.

## Worker protocol

Workers no longer scan the global task queue or decide which tasks are
eligible.

A worker:

1. heartbeats its identity, current generation, capabilities, resource
   capacity, and active assignment state to the hub;
2. fetches or receives an assignment whose lease already exists;
3. renews the lease and executes the assigned lifecycle stage;
4. returns evidence, completion, cancellation, or a structured preparation
   failure.

The worker's historical pull endpoint becomes assignment fetch/trigger: it may
request that the hub run an allocation round, then returns the assignment the
hub created for that identity. It does not apply `allowed_projects`,
`required_metadata`, canary-only filtering, or its own candidate ordering.

Worker-advertised project lists remain usable as affinity hints. Authorization
belongs to hub-owned tenant and trust policy, not to a worker's local command
line.

## Lifecycle and package behavior

Ordinary task edges express work ordering. Independent authoring nodes may run
in parallel. Repository-branch serialization is applied only where shared
integration or publication state actually requires it.

Work packages may contribute priority and dependency information, but they do
not own a second scheduler. Review, integration, testing, publication, and
finalization converge on the same dispatchable work-item model as authoring.
Real shared resources use explicit resource leases rather than broad
project-wide exclusion.

## Retained decisions and anomalies

Every authoritative allocation round is retained with:

- round identity, allocator version, start, completion, and duration;
- ready tasks and free capacity;
- assignments with exact task, agent, and lease IDs;
- unmatched runnable tasks and structured pair evaluations;
- transactional claim failures.

A selected-pair claim rejection is an error-level control-plane event. It is
never discarded as poll-level debug output. Repeated identical events may be
rate-limited, while their allocation-round records remain durable.

When runnable work and compatible free capacity coexist without an assignment,
MAC opens a ready-capacity mismatch episode. The episode becomes a warning
after five minutes and critical after ten minutes, and records its resolution.
Task lifecycle spans retain equivalent five- and ten-minute stage-stranding
signals.

Collisions over shared objects, including repository refs, integration slots,
publication barriers, and exclusive resources, are retained with the competing
tasks and wait duration. Telemetry records state changes and bounded rounds;
it does not restore high-volume per-candidate or empty-poll logging.

The operational proof is work completed through author, review, integrate,
test, publish, and canonical completion. Heartbeats, dry-run candidates, and
configuration activation are not completion evidence.

## Events and reconciliation

Task creation or release, dependency completion, lease release or expiry,
agent capacity change, project activation, and retry eligibility trigger an
allocation round. A short reconciliation tick is a recovery mechanism for
missed events, not a competing dispatcher.

If a task advertised as ready cannot be transactionally claimed, MAC treats
that disagreement as a control-plane defect. It retains the failure, continues
allocating other work, and opens or updates the appropriate repair signal.

## Removed behavior

The cutover explicitly removes:

- worker-local candidate scans and hard project/metadata/canary filters;
- separate push-dispatch and pull-dispatch allocation authorities;
- independent `ready` and `why-unclaimed` eligibility reconstruction;
- dual-window candidate scans and page-prefix fairness rotation;
- work-package-specific claim authority;
- silent catch-and-continue loops around transactional claim rejection;
- compatibility fallbacks that reinterpret malformed dependency IDs at read
  time.

Legacy fields may be migrated or ignored as documented affinities, but no
runtime path may restore them as hidden hard gates.

## Consequences

- The hub can account for all runnable work and all usable capacity in one
  snapshot.
- Readiness and real allocation share one definition.
- Workers become simpler and replaceable because they fetch work rather than
  schedule it.
- Task-graph defects are repaired once at the write boundary.
- Genuine safety boundaries remain fail-closed and auditable.
- Preferences and environment drift become ranking or preparation inputs
  instead of silent stranding.
- Allocation-round and lifecycle telemetry can measure ready-to-lease latency,
  false-ready defects, unused compatible capacity, collisions, and end-to-end
  throughput.
- The hard cutover accepts worker rebuilds and temporary fleet disruption in
  exchange for deleting the broken dual system.

## Alternatives considered

### Patch the abbreviated dependency case

Rejected. It would repair the incident while leaving multiple eligibility
authorities free to disagree on the next predicate.

### Keep worker pull as a second scheduler

Rejected. Worker-local knowledge cannot produce a globally work-conserving
allocation or an authoritative fleet explanation.

### Preserve legacy dispatch behind a fallback flag

Rejected. A fallback would keep both semantics alive, make telemetry
ambiguous, and allow the fleet to drift back to the broken path. Recovery is
fix-forward on allocator v2.

### Use an optimization solver or model-based router immediately

Rejected. Deterministic greedy matching is sufficient to restore throughput
and is easier to prove. More sophisticated ranking may be evaluated later
without changing the hard constraints or atomic claim boundary.

