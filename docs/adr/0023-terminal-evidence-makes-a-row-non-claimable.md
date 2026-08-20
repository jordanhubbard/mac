# ADR 0023 - Terminal evidence makes a row non-claimable, and retry needs lineage

- Status: **Proposed**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0013 (one authoritative hub allocator), ADR 0020 (a running task
  is not editable), ADR 0021 (schema changes need versioned migrations),
  ADR 0022 (a gate returns a named decision, not a boolean)
- Prior art: `NVIDIA-dev/horde-claw-fleet` — ADR-0121 (retry and terminal
  reconciliation semantics), findings 1, 2, 4 and 8

## Context

A task row's `state` is a queue position. It is not a statement about the
world. MAC treated it as one, and it cost us duplicate work twice over.

**The duplicate-PR bug.** On 2026-08-19 the open pull-request queue held 23
pull requests for 12 distinct pieces of work; one task emitted five divergent
implementations from two agents. The same session reproduced it by hand:
`task_f33a2da7` was implemented and merged as PR #498, then reopened on a fleet
restart without anyone checking that its work had landed, and a worker
immediately claimed it and began re-implementing a merged module.

MAC could already answer "has this work landed?" — but only at *completion*
time, inside `ControlPlane._require_canonical_integration_proof`, and only as a
raising gate. The claim and reopen paths could not ask the question at all.

horde-claw-fleet's ADR-0121 finding 4 names the rule:

> Terminal evidence and queue status diverged. Task 7f84dddd had terminal
> failure evidence but later appeared running or claimable again. Rows with
> terminal evidence MUST NOT be claimable unless an explicit replacement or
> retry row is created.

**One retry concept where there are five.** MAC's retry meant exactly one
thing: run the same task again. ADR-0121 findings 1 and 2:

> A stale retry still owned only `runner/src/cost.rs` and
> `runner/tests/test_telemetry_emission.rs` even though the prior failure
> evidence already proved that scope was insufficient.

> For scoped work, the next action should have been a revised task or graph
> node, not an in-place auto-retry.

Conflating them is why a MAC retry re-emitted a pull request against a scope
already proven insufficient.

**No lineage.** `--disposition superseded` hard-required a replacement *task*
and could not name a merged pull request, so supersession by a PR — the common
case for operator work — was inexpressible and ended up in free text. Nothing
could answer "what replaced task X".

## Decision

### 1. Terminal evidence is a claim gate, not only a completion gate

`mac.terminal_evidence` owns one non-raising detector over already-loaded task
metadata and evidence. Three kinds count, most to least authoritative:

| kind | proof |
| --- | --- |
| `canonical_integration` | the finalizer's remotely-verified guarded push |
| `merged_pull_request` | a forge reported the task's PR merged |
| `recorded_completion` | the row records a durable completion |

`_require_canonical_integration_proof` is now expressed in terms of the same
per-evidence predicate, so the completion gate and the claim gate cannot drift
apart. Two independent copies of "has this work landed?" is how a merged task
stayed claimable.

`ControlPlane.claim_task` refuses a row carrying terminal evidence. Per ADR
0022, the refusal names the evidence and the two legitimate next actions rather
than returning a bare denial.

### 2. Reopening such a row refuses, or replaces, and says which

`reopen_task(..., replace=False)` refuses and names the terminal evidence.
`replace=True` leaves the original terminal and creates a **replacement row**
carrying `lineage.retry_of` back to it, returning the replacement. Surfaced as
`mac task reopen --replace` and `POST /tasks/{id}/reopen {"replace": true}`.

The narrow exemption is deliberate: a row is claimable despite terminal
evidence exactly when it *is* the explicit replacement or retry row ADR-0121
requires, which its own lineage says. A bare `open` is never authorisation.

### 3. Five retry kinds, distinguished by one field

`mac.retry_kinds` separates what "retry" collapsed. The safety-relevant output
is `redispatch_same_scope`, and exactly one kind sets it:

| kind | re-dispatch scope? | when |
| --- | --- | --- |
| `task_retry` | yes, unchanged | transient failure |
| `validation_self_repair` | no (never left) | failure inside declared scope, attempt still live |
| `graph_amendment` | no, revised scope | the scope was wrong |
| `supersession` | no, someone else's | a replacement landed |
| `terminal_reconciliation` | no, never re-run | the work already exists |

`_auto_retry_blocked_attempt_task` — MAC's single in-place auto-retry — now
consults this. A scope failure sets `plan_first` and records a
`scope_amendment` rather than re-queueing the identical scope; a row with
terminal evidence is not re-queued at all.

`decide_retry_success_supersession` fires `SupersedePriorFailure` when a
replacement landed against a prior carrying terminal evidence.

### 4. Lineage is durable, bidirectional, and may name a pull request

`mac.task_lineage` records three forward relations from the successor's point
of view — `retry_of`, `amends`, `replaces` — under `metadata.lineage`
(`mac.task_lineage.v1`). A target is a task id **or** a pull request (URL or
`owner/repo#number`). `replacement_pull_request` joins `replacement_task_id` in
the cancellation contract; the liveness guard is skipped for a PR, because a
merged pull request has already done the work, which is strictly stronger than
being live.

Queryable via `ControlPlane.task_lineage`, `mac task lineage`, and
`GET /tasks/{id}/lineage`. Legacy `repository_ref_lifecycle.replacement_task_id`
pointers are projected into the same view, so rows cancelled before lineage
existed stay queryable. Per ADR 0021 this is additive metadata under a
versioned schema key, so no destructive migration is required.

### 5. Process cleanup keys on recorded ownership, never on text

ADR-0121 finding 8:

> Manual cleanup attempted to kill a stale task by grepping process command
> lines for an old task id. That id also appeared in ACTIVE replacement-task
> prompts as contextual text, so active tasks were killed too.

That failure mode is now *structurally* unavailable rather than discouraged.
`mac.process_ownership.ProcessOwnership` has no field that can hold a command
line, prompt, or argv; passing one is refused where the mistake is made. The
selector compares the recorded `task_id` for equality and nothing else, so
there is no code path from text to a signal.

This is forward-looking. MAC's abort works by lease revocation and worker
polling, which is safe and unchanged (ADR 0020). The module exists so that
anything which later reaches for a process tree cannot reintroduce the
incident. `tests/test_process_ownership_kill_selection.py` asserts the ADR-0121
case directly: a task id quoted in another task's prompt cannot select it.

## Consequences

- A merged-and-reopened row now refuses its claim instead of producing a second
  implementation. This is the intended breaking change; the escape hatch is
  `--replace`, which is one command and leaves an audit trail.
- `_auto_retry_blocked_attempt_task` reads a task's evidence once per row that
  actually reaches auto-retry. That set is small and bounded, and no other
  point in the sweep can see that a blocked row's work already merged.
- The five retry kinds are decided in a pure module, so the policy is testable
  without a control plane and reviewable without reading the sweep.
- A replacement row starts with a clean attempt ledger: it inherits project,
  priority, capabilities and metadata, but not the original's lease, ref
  lifecycle, or recorded failure class.

## Alternatives considered

**Detect duplicates at publication instead of at claim.** Cheaper, and it would
have caught the 23-PR queue — but only after 23 agents had each spent a full
attempt budget re-implementing merged work. The cost of a duplicate is paid
before publication, so the gate belongs at claim.

**Keep one retry concept and special-case timeouts.** This is roughly where MAC
already was: `plan_first` injection handled the timeout case only, and every
other scope failure re-dispatched unchanged. Naming the five kinds makes the
absent cases visible rather than leaving them as an undiscovered gap.

**Let `superseded` keep requiring a replacement task, and file a stub task for
each merged PR.** A stub whose only content is "this is really PR #498" is a
row nobody works, which the dispatcher must then learn to ignore — inventing a
second non-claimable-row problem to avoid solving the first.
