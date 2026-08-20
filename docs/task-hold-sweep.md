# Advancing held tasks toward terminal states

A held task has no owner and no clock. Nothing revisits it, so a hold outlives
its reason indefinitely. The hold sweep is that clock: a periodic, budgeted
pass over parked and stalled work that decides the next appropriate state and
moves the task there.

It is the interval-triggered companion to claim-time triage. Same judgement,
different trigger: triage runs once, for the task an agent is about to work;
the sweep runs on a timer over everything that is parked and going nowhere.

## Why this exists

Measured on 2026-08-20, of 81 open tasks in project `mac`:

| Population | Count |
| --- | ---: |
| Carrying `metadata.no_dispatch` | 75 |
| Parked pending a decomposition fix that had already merged | 17 |
| `open` with `attempt_count >= max_attempts` | 4 |

The holds accumulated steadily — 23 on 08-17, 9 on 08-18, 19 on 08-19 — and
nothing noticed when their reasons went away.

The four exhausted tasks were worse than parked. They were permanently
undispatchable AND invisible: they read as ordinary open work in every list
view, and `mac task reopen` silently does nothing to them because reopen
requires a terminal state. Raising `max_attempts` on one released it and an
agent claimed it within seconds, which is how we know dispatch was never the
problem.

## What a run does

Each examined task gets exactly one verdict, and every verdict is recorded on
the task under `metadata.hold_review`:

| Verdict | When | Effect on the task |
| --- | --- | --- |
| `released` | the condition that justified the hold is gone | `no_dispatch` cleared, citing what satisfied it |
| `budget_raised` | attempts exhausted and the work is still wanted | `max_attempts` raised, deliberately and once |
| `cancelled` | superseded, already landed, or no longer wanted | closed, naming the replacement or the change |
| `reviewed_still_valid` | re-examined and re-justified | nothing changes except the record |
| `undecidable` | the sweep cannot decide cheaply | nothing changes; it is reported |

`reviewed_still_valid` is not a no-op. A hold somebody re-justified this
morning and a hold nobody has looked at since August are the same row until
one of them carries a review record.

`open` + attempts exhausted is the one outcome the sweep may never leave in
place. It either raises the budget (bounded by
`MAC_HOLD_SWEEP_MAX_ATTEMPT_GRANTS`) or moves the task to a terminal state,
saying which and why.

## What a hold must declare to be decidable

The sweep reasons about `metadata.hold`:

```json
{
  "hold": {
    "reason": "waiting on the decomposition fix",
    "until_task": "task_<32 hex>",
    "replacement_task_id": "task_<32 hex>",
    "disposition": "not_wanted",
    "until_change": {"subject": "Fix the decomposition guardrail",
                     "commit": "<40 hex>", "merged": true},
    "satisfied_by": {
      "subject": "MAC task task_<32 hex>: land the fix",
      "branch": "mac/<agent>/task_<32 hex>",
      "commit": "<40 hex>",
      "merged": true
    }
  }
}
```

Every field is optional, and a hold that declares none of them is reported
`undecidable` rather than guessed at.

| Field | Claim | Outcome when it holds |
| --- | --- | --- |
| `until_task` | "release me when that task completes" | released |
| `until_change` | "release me when that change lands" | released |
| `replacement_task_id` | "this work moved to that task" | cancelled, superseded |
| `satisfied_by` | "this task's own work already landed" | cancelled, not applicable |
| `disposition: not_wanted` | "nobody wants this any more" | cancelled, with the reason |

`until_change` and `satisfied_by` are deliberately separate. One says the
blocker merged, the other says this task's work is done; conflating them would
close a task the moment its blocker landed. Only `satisfied_by` is held to the
task-attribution rules below, because only it claims to BE this task's work.

Releasing a task whose attempts are exhausted also raises its budget in the
same action. A release without one would hand back an open, undispatchable row
— the invisible state, created by hand.

## Why the attribution rules are strict

The obvious way to find "work that already landed" is to look for a merged PR
mentioning the task id. On 2026-08-20 that returned 24 hits out of 75 and every
one was a false positive — pull requests cite task ids as evidence, in their
bodies, for unrelated work. Tightening it to "the id is in the PR title or
branch" returned zero, correctly, because a held task is never claimed, so no
PR is ever opened for it. Both signals are wrong, in opposite directions.

So a citation only satisfies a close when the change identifies itself AS this
task's work — the id in the commit subject, the PR title, or the branch name —
AND the change has landed. An id appearing only in a body or trailer is
classified `mention_only` and the task is reported undecidable instead. An
unexplained automated close is indistinguishable from work being lost.

## Interval, budget, and idempotency

This is archaeology over the whole backlog, so it runs on its own hours-scale
timer rather than on the dispatch tick.

* **Bounded budget.** `MAC_HOLD_SWEEP_BUDGET` caps the tasks examined per run.
  A run reports `deferred_over_budget` so a truncated sweep never reads as a
  clean backlog.
* **Cheap skip.** Each verdict records a fingerprint of the hold reason and the
  facts the decision read. An unchanged task inside
  `MAC_HOLD_SWEEP_REVIEW_TTL_SECONDS` is skipped without re-deciding it.
* **Fair ordering.** Never-reviewed tasks are examined first, then the least
  recently reviewed, so the budget rotates through the backlog instead of
  re-deciding the same head forever.
* **Two concurrent runs are safe.** A run holds a non-blocking lock, every
  action re-reads the task and re-checks its fingerprint immediately before
  writing, and every action is idempotent on its own.

## Operating it

```bash
mac fleet hold-sweep status    # config + last run report
mac fleet hold-sweep run       # one budgeted pass now
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAC_HOLD_SWEEP_ENABLED` | off | the sweep is a no-op until this is set |
| `MAC_HOLD_SWEEP_INTERVAL_SECONDS` | 21600 (6h) | how often a scheduled run fires |
| `MAC_HOLD_SWEEP_INITIAL_DELAY_SECONDS` | 300 | delay before the first run after start |
| `MAC_HOLD_SWEEP_BUDGET` | 50 | tasks examined per run |
| `MAC_HOLD_SWEEP_REVIEW_TTL_SECONDS` | 604800 (7d) | how long a verdict suppresses re-examination |
| `MAC_HOLD_SWEEP_ATTEMPT_GRANT` | 1 | extra attempts per grant |
| `MAC_HOLD_SWEEP_MAX_ATTEMPT_GRANTS` | 1 | grants before an exhausted task must go terminal |
| `MAC_HOLD_SWEEP_PROJECT` | all | restrict the sweep to one project |

An out-of-range value disables the sweep and reports a configuration error
rather than silently substituting a default.
