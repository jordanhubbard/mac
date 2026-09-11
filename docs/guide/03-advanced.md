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

A successful executor exit is not proof that the request was satisfied.
**Evidence**, **review**, **publication**, and **operator acceptance** record
different facts.

- Evidence is typed. A `code` deliverable expects a repository change; a
  `report` deliverable is satisfied by an `operator_result` — a substantive
  summary with no diff. That distinction exists so a non-code task cannot be
  closed by a diff, and a code task cannot be closed by prose.
- The default review workflow runs on the hub's publication worker, clones the
  repository, and runs a contract gate. That validates the recorded tests, not
  every behavior in the request. Qualifying non-repository results can follow
  a deterministic approval path; do not read that as independent user acceptance.
- `mac task outcome <id>` separates these facts. `mac task accept` records an
  operator decision against the current executor evidence and attempt. It
  cannot bypass review, complete a task, or prove deployment.

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

## Recovery and delivery limits

AgentBus broadcasts supply context between tasks. Harness hooks also provide
partial in-flight delivery; support varies by harness and message type. A
queued message is not proof that the executor read or acted on it. Consult
[the hook implementation status](../adr/0032-cli-session-hooks-not-tmux.md)
and inspect acknowledgments. For a required stop, use the authenticated
`mac task stop` lifecycle operation, which checks executor abort, rather than
relying on conversational delivery.

Use `mac task why-unclaimed <id>` and `mac task throughput` to distinguish live
dependencies, failed blockers, held work, and publication parks. Recovery is
state-specific: neither repeated publication sweeps nor cancelling a failed
dependency guarantees progress. The [trust workflow](06-trust-workflow.md)
shows the operator handoff and the evidence to preserve before retrying.

An idle identity is not execution capacity. Throughput reports baseline
allocator-eligible workers separately from idle identities, with exclusion
reasons. A baseline-eligible worker may still fail a particular task's
capability, hardware, project, or tenant requirements; inspect the task's
allocation explanation before changing fleet size.

## Operating notes

- **`MAC_REVIEW_TICK_LIMIT`** caps how many tasks the publication sweep
  advances per cycle. Raising it to work around a starved sweep gives the
  starving task more slots too; fix the cause instead.
- **The hub serves deployed code**, not the operator's checkout. Verify the
  deployed revision and the running process after a rollout; a source update
  alone does not prove that the process loaded it.
- **Schema changes use versioned migrations.** `mac.schema_migrations` owns
  the ordered checksum ledger and transactional application. Deploy invokes
  `mac-schema-migrate`; ordinary startup verifies the schema and fails on
  drift. Editing the bootstrap `schema.sql` is not an upgrade procedure. See
  [the migration contract](../adr/0021-schema-changes-need-versioned-migrations.md).
