# Structured task bodies: actions on a Component

Status: design note, nothing implemented. Written 2026-08-02 after reading
`~/Src/literate-ai` alongside MAC's existing workflow and work-package models.

## The question

A MAC task body is a title plus free text plus a metadata blob. Could a task
instead name **an action on a Component** — a closed verb applied to an
addressable thing — so the body is constrained and machine-checkable?

## What literate-ai supplies

`literate-ai` is a spec-led framework whose domain model already has the two
things MAC's task body lacks.

A **closed verb set**, as a versioned DAG of typed stages:

```text
resolve -> source -> index -> contracts -> plan -> generate -> validate
                                             ^                    |
                                             +------ repair <-----+
       -> classify -> authorize-build -> build -> package -> publish
```

A **lifecycle state machine** that defines what "done" means:

```text
defined -> specified -> resolved -> source-ready -> origin-verified -> indexed
  -> planned -> generated -> validated -> classified -> build-authorized
  -> built -> packaged -> published
```

Components are addressable (`component://<namespace>/<name>`), revisions bind
that coordinate to exact specification and source identities, every transition
produces an immutable object and event, and a transition may be retried only
when its idempotency key and input identities match.

A structured MAC task body would then be:

```yaml
action:
  component:  component://<namespace>/<name>
  revision:   <exact input identity>
  transition: generate | validate | classify | build | package | publish | ...
  target:     <lifecycle state>
```

## What it would buy, against failures this repository has actually measured

**Decomposition becomes derived instead of invented.** The planner currently
generates children by model judgment — the recurring `implement / test /
verify` shape. That shape produced the dependency chains that stranded 76
integration parents (see `task_0ee0b7ce`, `task_5e4634e8`). If a task is
`(component, target_state)`, its children are the lifecycle transitions
between current and target state: a computed path through a known DAG. A task
that is already one transition is *structurally* indivisible, which is
`task_0ee0b7ce`'s acceptance criterion (a) obtained by construction rather
than enforced by a rule.

**Dependencies become derived instead of authored.** They currently cost 5.3x
completion (`docs/archive/field-notes/assessment-2026-08-02.md` section 3) and are attached by the
planner. Lifecycle order already implies them — `build` cannot precede
`authorize-build` — so the edges are guaranteed acyclic and terminating.

**Completion becomes checkable instead of asserted.** The assessment records
an executor reporting "56 passed total" while `pushed=false`, and a
`verification_contract_failed` cohort of 547. With a target state, done means
the immutable transition object exists at that state. That is a stronger
contract than an evidence blob an executor fills in about its own work.

**Retry gets a real key.** literate-ai retries a transition only when the
idempotency key and input identities match. MAC currently approximates this
with `attempt_count`, failure fingerprints, and a repair-recursion guard.

## What MAC already has — and this is the important finding

MAC is much closer to this than the free-text task body suggests. Two existing
subsystems already carry most of the skeleton.

`workflow_models.WorkflowNode` is a typed DAG node, but its vocabulary is
**process governance**, not domain transitions:

| | value |
|---|---|
| `NodeType` | `task`, `approval`, `commit`, `verify`, `plan` |
| `EdgeCondition` | `success`, `approved`, `rejected`, `failure`, `timeout`, `escalated`, `cancelled` |
| body | `instructions: str` — free text |

The `plan` node type exists precisely to "translate the workflow's free-form
input description into structured payloads for downstream nodes" — i.e. MAC
already recognises the gap and fills it at runtime with a model call.

`work_package_models` goes further and is deterministic:

- node types `analysis`, `mutation`, `integration`, `certification`, with
  aliases from the workflow types;
- **`WorkPackageEffects`: `reads` / `writes` / `exclusive` / `external`** —
  each node declares the resources it touches, and allocation policy uses it;
- `expected_outputs`;
- canonical compilation to a plan digest, so the same proposed plan compiles
  identically in an API process, a worker, or an offline audit;
- bounded at 100 nodes and 100 dependencies;
- `validate_executable_work_package_effects`, requiring a controller-owned
  fence for external effects.

So MAC already has a structured node body. Its structure axis is **resource
effects plus process governance**. literate-ai's is **domain lifecycle**.
Those are orthogonal, not competing.

## The actual gap

One thing is missing from the work-package node: a closed domain verb and a
target state. `node_type` says what *kind* of step it is; `instructions` says
what to do, in prose. Nothing says *what this step does to what artifact*.

That makes the change additive rather than a new subsystem:

1. Add an optional `action: {component, transition, target}` to a
   work-package node (or to task metadata for the simple case).
2. **Derive `effects` from it.** A `build` transition reads the source
   snapshot and writes the build artifact. Effects are currently hand-declared
   and drive allocation and conflict policy, so deriving them removes a class
   of hand-authoring error, not just prose.
3. Derive dependency edges from lifecycle order.
4. Define completion as the target state being reached.

## Where this does not fit, and why it must stay optional

Most work in this ledger is not a transition on one component. From the open
set at the time of writing:

- *Retire the Hermes vendor and rename by function* — 594 files, a live
  gateway, a coordinated rename across persisted config and deployed command
  lines.
- *The contract gate fails its own preflight on Python 3.13+* — a defect in
  the tooling around components.
- *Re-measure the dependency yield table* — a measurement.
- *Deploy the backup fixes* — an operation on a running fleet.

None reduce to `generate(component X)`.

And the number that should govern the whole idea: **`direct_task` — plain
human free text — is the best-performing origin at 20.4%.** The generators
that filed *structured, machine-authored* work ran at 0-3% and have since been
retired behind a yield gate (PR #267). Structure is not what made work
complete. Constraining the body risks damaging the one input channel that
demonstrably works.

Work packages are also a heavyweight construct — durable, parallel, capped at
100 nodes. The fit is substantial multi-step work, not "fix this typo".

## Recommended first step

Do not build a new task kind. Establish whether the existing work-package
pipeline can already express `(component, transition, target)` by adding the
optional `action` field and deriving `effects` from it for one real workflow.

Then measure, using the method in `task_e48d4eed`: completion yield of
structured versus free-text tasks, as a cohort, not a cumulative table.

This session twice produced a design that measurement inverted — the
strand-recovery sweep as first designed freed *zero* tasks, and the
dependency-yield "improvement" was unmeasurable on a cumulative metric. A
structured-task-body change is exactly the kind of plausible architecture that
should not be built before its benefit is measured.

## References

- `docs/archive/field-notes/assessment-2026-08-02.md` sections 3 and 4 — dependency and origin yield
- `src/mac/workflow_models.py` — `NodeType`, `EdgeCondition`, `WorkflowNode`
- `src/mac/work_package_models.py` — `WorkPackageEffects`, canonical plan digest
- `~/Src/literate-ai/docs/architecture/domain-model.md` — lifecycle state
  machine, `WorkflowDefinition`/`WorkflowRun`, ports
