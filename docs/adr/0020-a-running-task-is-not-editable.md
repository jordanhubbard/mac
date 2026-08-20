# ADR 0020 - A running task is not editable; stop it first

- Status: **Proposed**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0013 (one authoritative hub allocator), ADR 0016 (agents decide
  what a task needs), ADR 0019 (privilege is an ACL on a resource tree)

## Context

`mac task update` edits a task in place regardless of its state. Applied to a
RUNNING task this produces a split brain: the ledger holds one description and
the executor is working from another, read at claim time. Nothing reconciles
them, and nothing tells the agent that the thing it is building has changed.

This is not theoretical. On 2026-08-20 a task's acceptance criteria were
rewritten while an agent was executing it. The agent had already produced
correct work against the original text; the rewritten criteria described
something else, and the only reason a reviewer did not reject correct work was
that the discrepancy was noticed by hand. A second task had a dependency added
while RUNNING and stayed RUNNING with an unmet dependency: the ledger said it
was blocked, the executor kept going.

### What exists today, verified

**There is no task pause or resume.** `TaskState` has eleven values — OPEN,
WAITING, BLOCKED, CLAIMED, RUNNING, NEEDS_REVIEW, NEEDS_INPUT, REVIEWING,
COMPLETED, FAILED, CANCELLED — and none of them means "an operator is holding
this while it is edited".

**`ask`/`answer` (NEEDS_INPUT) is the closest park/resume pair, and it already
has the dispatch property this needs.** From `request_task_input`:

> This is NOT a failure path. The work is still wanted; it simply cannot
> proceed until a person answers. Parked tasks are excluded from every
> sweeper, reaper, and dispatch pass, so they wait indefinitely instead of
> burning their attempt budget on a blocker no code path can clear.

Excluded from every dispatch pass, no attempt burned, waits indefinitely —
exactly the "stopped rather than claimable" semantics wanted here. But it is
wrong on two counts. Its *meaning* is "waiting on a human answer", and
`mac task needs-input` is documented as the operator inbox, so parking edits
there corrupts the one queue an operator is supposed to be able to trust. And
critically, it does **not** stop the executor: `request_task_input` calls
`_transition_task_internal` and returns. The state changes; the agent keeps
running.

**`cancel` is the only primitive that actually aborts a running executor** —
"actively cancel a task, revoke its lease, and abort a running worker
executor". But it is terminal. Using it as an abort files live work under
CANCELLED, which already holds 3,533 tasks and is therefore already unusable
as a measure of what was genuinely abandoned.

**`mac agent hold` does not help.** Verified during a fleet stop the same day:
holding every agent stops new dispatch and leaves in-flight work running. Hold
is a dispatch gate, not a drain.

So the two halves exist in different primitives — abort lives in `cancel`,
not-claimable-and-not-reaped lives in NEEDS_INPUT — and neither composes into
an edit cycle without mislabelling the task.

## Decision

### 1. Editing a RUNNING task is refused

`update_task` refuses to modify a task in RUNNING (and CLAIMED, which has the
same split-brain exposure: an agent holds the lease and is about to read the
task). The refusal names the verb to use instead. This is enforced in the
service layer, not the CLI, so hub-mode callers cannot bypass it.

Refusing is the point. The current behaviour is not "flexible"; it silently
produces two versions of the truth.

### 2. A new state: STOPPED

STOPPED means **an operator is holding this task; nobody else may take it.**
It inherits NEEDS_INPUT's dispatch semantics exactly — excluded from every
sweeper, reaper and dispatch pass; no attempt budget consumed; waits
indefinitely — and it is distinct from NEEDS_INPUT so the operator inbox keeps
meaning "someone must answer a question".

It is explicitly **not** terminal. A stopped task is live work.

### 3. `stop` is one atomic operation

    mac task stop <id> --reason ...

Abort the running executor, revoke the lease, and transition to STOPPED, as
one operation that either completes or leaves the task untouched. The three
steps must not be separately observable: an agent that keeps running against a
task the ledger has already re-labelled is the split brain this ADR exists to
remove.

### 4. Edit, then start

While STOPPED the task is freely editable — that is the state's purpose. Then:

    mac task start <id>

returns it to OPEN (or WAITING if its dependencies are unmet, evaluated at
that moment rather than assumed). The next agent to claim it reads the edited
task, because there is no longer an agent holding a stale copy.

### 5. Abort delivery is bounded and its outcome is definite

An executor may be unreachable — the host is gone, the sandbox is wedged, the
process is ignoring signals. `stop` therefore carries a bounded deadline. On
expiry it does not silently succeed: it reports that the abort was not
confirmed and leaves the task in STOPPED with the unconfirmed abort recorded,
so an operator can see that a process may still be running against it.

"Probably stopped" must be distinguishable from "stopped".

## Consequences

- An operator can correct a task's scope without racing its executor, and
  without cancel-and-refile, which loses the id, history, attempts and
  evidence.
- CANCELLED starts meaning abandonment again, because aborts stop landing
  there.
- One more state to reason about. Mitigated by giving it the same dispatch
  treatment as NEEDS_INPUT rather than inventing new rules: everything that
  already skips NEEDS_INPUT skips STOPPED, and a test should assert that the
  two sets stay identical.
- A task can be stopped and forgotten. STOPPED work is invisible to every
  sweeper by design, so it needs to be visible to a person: `mac task list`
  must show it prominently, and stopped-for-a-long-time is worth a report.
- Under ADR 0019, `stop` and `start` are `control` on the task, and editing
  while stopped is `write`. An agent's task-scoped credential carries neither,
  so an agent cannot stop its own task to escape a gate.

## Alternatives considered

**Reuse NEEDS_INPUT for editing.** Rejected: it means "a person must answer a
question", and `mac task needs-input` is the operator inbox. Overloading it
makes that inbox untrustworthy, and untrustworthy queues stop being read.

**Let the edit land and notify the running agent.** Rejected: it makes every
executor responsible for re-reading and reconciling mid-flight, and there is
no defined behaviour for work already done against the old text. The failure
mode observed — correct work measured against criteria written after it — is
exactly what this produces.

**Keep cancel-and-refile.** Rejected: it loses the task id, history, attempt
count and evidence, and breaks every reference from other tasks and PR titles.
Observed consequence: the edge simply never gets added.

**Compose `cancel` then `reopen`.** This is what an operator does today. It
works, and it is wrong: the task passes through CANCELLED, so the ledger
records abandonment that did not happen, and the two steps are separately
observable — between them the task is terminal and a sweeper may act on it.
