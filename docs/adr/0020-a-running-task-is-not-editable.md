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

### 1. `update` is atomic at the task layer

A RUNNING task is never modified in place. But rather than refusing the edit
and making the caller compose stop -> modify -> start, `update_task` performs
that cycle ITSELF, as one operation, when the task is RUNNING or CLAIMED.

The caller asks for an update and gets an update. Underneath, the hub aborts
the executor, revokes the lease, applies the change, and restarts the task —
atomically, so no intermediate state is observable and no caller can get the
sequence wrong or abandon it half-done. An earlier draft of this ADR required
the three steps not to be separately observable; making the cycle a single
lower-layer operation is what makes that structural rather than a rule callers
must remember.

An operator composing the cycle by hand is how a task ends up stopped and
forgotten, or edited and never restarted. The atomic form has no such state.

CLAIMED is included deliberately: the agent holds the lease and is about to
read the task, so it has the same split-brain exposure as RUNNING.

### 2. A new state: STOPPED

STOPPED means **an operator is holding this task; nobody else may take it.**
It inherits NEEDS_INPUT's dispatch semantics exactly — excluded from every
sweeper, reaper and dispatch pass; no attempt budget consumed; waits
indefinitely — and it is distinct from NEEDS_INPUT so the operator inbox keeps
meaning "someone must answer a question".

It is explicitly **not** terminal. A stopped task is live work.

### 3. `stop` and `start` also exist standalone

    mac task stop <id> --reason ...
    mac task start <id>

`update` covers the edit cycle, but stopping has standalone value: halting
runaway work, freeing an agent, or pausing something to investigate it without
changing anything. `stop` aborts the executor, revokes the lease and
transitions to STOPPED as one operation that either completes or leaves the
task untouched.

These are separate ACL permissions from `update` (ADR 0019). Authorising
someone to correct a task's description should not authorise them to halt the
fleet's work, and `stop` should not imply `start` — a principal trusted to
halt runaway work is not thereby trusted to release it back.

### 4. A STOPPED task is freely editable, and `start` re-evaluates

STOPPED is the one state in which editing is unrestricted — that is its
purpose. A task stopped standalone can be edited by any number of `update`
calls before being started; those calls take the ordinary path, because the
task is not running and there is no executor to contradict.

`start` returns it to OPEN, or to WAITING if its dependencies are unmet,
**evaluated at that moment** rather than assumed. This matters because the
edit may have added a dependency: a task edited into a blocked shape must come
back as blocked, not as claimable. The next agent to claim it reads the edited
task, because no agent is holding a stale copy.

### 5. Abort delivery is bounded and its outcome is definite

An executor may be unreachable — the host is gone, the sandbox is wedged, the
process is ignoring signals. `stop` therefore carries a bounded deadline. On
expiry it does not silently succeed: it reports that the abort was not
confirmed and leaves the task in STOPPED with the unconfirmed abort recorded,
so an operator can see that a process may still be running against it.

"Probably stopped" must be distinguishable from "stopped".

### 6. A restarted task is re-entered from the top

`start` does not resume. The next agent to claim the task enters it at the
beginning of the ordinary evaluation loop and re-derives everything from the
task as it now stands.

This is the whole reason to stop a task rather than edit it in flight. The
edit exists to change something material — the description, the acceptance
criteria, the priority, the dependency set, the child decomposition. An agent
that resumed from where the last one left off would be acting on conclusions
drawn from the *previous* version of the task, which is the split brain moved
from mid-flight to across-attempts rather than removed.

Concretely, on re-entry the agent re-runs the decisions ADR 0016 puts in its
hands: is this atomic or does it need decomposing, does it need review, does
it need hardware I do not have, am I the right agent for it. It re-reads the
description and criteria. It re-evaluates dependencies and children as they
are now, not as they were.

Three things follow, and each is a way this can go wrong quietly:

- **Evidence from the aborted attempt is retained as history, not as current
  state.** It stays attached and queryable — an operator must be able to see
  what the previous attempt did — but nothing may treat it as satisfying a
  requirement of the restarted attempt. A `plan_decomposed` from before the
  edit describes a decomposition of a task that no longer exists.

- **A workspace or worktree left by the aborted attempt is not silently
  inherited.** Reusing it is resumption by the back door: the agent picks up
  a half-finished tree without having decided that the half-finished work is
  still the right work. If it is reused it must be an explicit decision the
  agent records, not a default of finding a directory already there.

- **The attempt counter counts attempts, not restarts.** An operator stopping
  a task to correct its scope must not consume the task's retry budget; the
  task failed at nothing. Conversely a restart must not reset the counter to
  zero, or a task edited repeatedly could never exhaust its attempts and would
  become unkillable. Restarts are recorded separately from attempts.

The test that matters: stop a task mid-execution, change its acceptance
criteria and its children, start it, and assert the next agent's first
recorded decision was derived from the new criteria — not that it merely
finished successfully. A task can finish successfully while having built the
wrong thing, which is exactly the failure that prompted this ADR.

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
- Under ADR 0019 the permissions are `update`, `stop` and `start`, split out
  of `write` and `control` precisely so this cycle can be granted without
  destructive or lifecycle authority. An agent's task-scoped credential
  (`read`, `append`, `create`) carries none of them, so an agent can neither
  stop its own task to escape a gate nor rewrite the criteria it is being
  judged against.
- Re-entry from the top means a stopped-and-started task pays its evaluation
  cost again. That is the price of the edit being meaningful, and it is small
  next to an agent completing the wrong work confidently.

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

**Refuse the edit and make the caller compose stop -> modify -> start.**
Rejected in favour of making `update` atomic at the task layer. Refusing is
defensible and was this ADR's first draft, but it pushes atomicity onto every
caller: a composed cycle can be abandoned between steps, leaving a task
stopped and forgotten, and each caller must remember the right order. Putting
the cycle below the API makes the guarantee structural. The standalone verbs
remain for the cases that genuinely are just a stop or just a start.

**Resume the restarted task where the previous attempt stopped.** Rejected:
it preserves conclusions drawn from the pre-edit task, which moves the split
brain from mid-flight to across-attempts instead of removing it. If the edit
did not change anything material there was no reason to stop the task; if it
did, the previous attempt's reasoning is exactly what must not survive.

**Compose `cancel` then `reopen`.** This is what an operator does today. It
works, and it is wrong: the task passes through CANCELLED, so the ledger
records abandonment that did not happen, and the two steps are separately
observable — between them the task is terminal and a sweeper may act on it.
