# Advanced Concepts

The mechanisms that decide whether work actually lands, and the places where
mac currently cannot do something it looks like it should. The gaps are listed
because a system that hides them is one you cannot operate.

## Leases: how one task stays one task

A worker does not "own" a task, it holds a **lease** on it. One live lease per
task, enforced by a unique index. The lease has an expiry, renewed by
heartbeat; a worker that dies stops renewing and the task returns to the pool.

This is why a task can be re-claimed while the previous attempt's process is
technically still alive: the lease expired, and the lease is what counts. It
also means lease expiry is a *retry*, not an error — and a task whose work
cannot land will be retried until `max_attempts`, which is exactly how one task
can produce many pull requests.

## Evidence and review

Work is not accepted because an agent says it succeeded. It is accepted because
**evidence** was recorded and a **review** judged it.

- Evidence is typed. A `code` deliverable expects a repository change; a
  `report` deliverable is satisfied by an `operator_result` — a substantive
  summary with no diff. That distinction exists so a non-code task cannot be
  closed by a diff, and a code task cannot be closed by prose.
- The default review workflow runs on the hub's publication worker, clones the
  repository, and runs a contract gate.

## Publication: the hard part

A task is not complete when a pull request opens. It is complete when the work
is on the canonical branch and the ledger can *prove* it:

```
repository task completion requires durable canonical integration proof
(canonical_integration.status=pass, remote_verified=true, and a matching
canonical branch SHA)
```

That proof is attached by the publication pipeline. Work landed by hand — a
human opening and merging a PR — does not produce it, and such a task cannot be
marked completed even with `force-complete`. This is deliberate: the gate
refuses to believe a claim it cannot verify.

### The native merge queue

GitHub merge queues are organization-only, so personal repositories get no
forge-side serialization. mac provides its own, with two properties worth
knowing:

- **Tree identity, not SHA identity.** An entry records the tree it was tested
  against and refuses to land unless the canonical tip's tree matches
  byte-for-byte. This is what makes speculation safe and what survives a squash
  merge changing the SHA.
- **Fail toward not landing.** Every failure kind — deferred, waiting,
  unreadable state, slot lost, speculation unavailable — routes through the
  existing backoff. None can reach "merge anyway".

The AIMD window grows by one on a land and halves on a failure, floor 1 and
ceiling 4. `MAC_MERGE_QUEUE_WINDOW_CEILING=1` disables speculation without
disabling the queue.

## Dispatch: why a task is or is not claimable

Claimability is a conjunction, and all of it must hold:

- state is `open`
- no unfinished dependencies, under the task's **join policy**
- no `no_dispatch` hold
- the project is not paused
- some agent's **capabilities** cover the requirement
- some agent's **hardware** matches

### Join policy is the subtle one

- `all_success` (the default): only a **completed** dependency releases the
  parent. A `failed` or `cancelled` blocker holds it forever.
- `all_settled`: any terminal dependency releases it.

Under `all_success`, cancelling a stuck dependency does **not** free its
dependents. This is the mechanism behind large blocked backlogs.

## Observability

Every state change writes history; the hub emits `action_events` and
`observability_events`. Signals are derived and named, e.g.
`high_token_work_without_publication` (a task burning model time without
landing) and `command_failure_churn` (repeated terminal command failures).

**A caution learned the hard way:** `action_events.timestamp` is a **text**
column holding ISO-8601 with a `T`. Comparing it against
`(now() - interval '1 hour')::text` silently matches everything, because the
cast produces a space-separated string and `'T' > ' '`. Filter with an
ISO-formatted literal instead. The same trap has produced wrong retention
metrics and wrong incident rates.

## Known gaps

These are real, measured, and tracked. They are here so you can operate around
them.

### AgentBus is write-only

Workers emit typed events (`git.pushed`, `task.claimed`, `capacity.saturated`
and others) from their own git and task paths. **Nothing reads them back** —
searching `src/`, `scripts/` and `deploy/` for a consumer finds only the
`mac admin agentbus read` command a human types.

Consequence: an agent cannot learn that its own work has landed. This produced
a task that opened eight pull requests across eight leases, one every ~30
minutes, before exhausting `max_attempts`. There is also no terminal
`git.merged` event to learn from even if something were listening.

### What the hub cannot do yet

The hub *detects* runaway conditions — the signal above fired on that task
while it was happening — but cannot act. The AgentBus lifecycle vocabulary has
`fleet`, `project`, `agent` and `task` scopes, and the verbs `stand_down`,
`abort`, `pause`, `resume`, `status`; but nothing consumes a directive, and the
hub has no authority to issue one. Detection without a channel is a report
nobody reads.

### Permanent failures are retried as transient

Publication treats every failure as retryable. That is right for a network
error, a lease conflict or a full queue window, and wrong for a reviewed commit
that no longer exists:

```
git publication verify_commit failed:
fatal: Not a valid object name <sha>^{commit}
```

No number of retries makes that SHA resolve. Until classification lands, a task
in this state consumes publication sweep slots indefinitely. The mitigation is
to park it: `mac task ask <id> --question "..."` moves it to `needs_input` and
out of the sweep.

### Dead dependencies are indistinguishable from live ones

A task blocked on a `failed` dependency looks exactly like one legitimately
sequenced behind live work, in every list view. Nothing computes whether a
blocker can still arrive.

## Operating notes

- **`MAC_REVIEW_TICK_LIMIT`** caps how many tasks the publication sweep
  advances per cycle. Raising it to work around a starved sweep gives the
  starving task more slots too; fix the cause instead.
- **The hub serves code from `~/.mac/src/mac`**, not from a developer checkout.
  Deploy is a fast-forward there plus a supervisor restart, and it is verified
  by PID change rather than checkout SHA — `refresh-source` has reported
  `restart_requested: false` while the checkout advanced.
- **`schema.sql` is `CREATE TABLE IF NOT EXISTS` with no migration framework.**
  It creates missing *tables* on restart, so a new table provisions itself. It
  is a **no-op for a new column on an existing table** — those need a
  hand-applied `ALTER`, and `migrations/` is manual-only; nothing reads it.
