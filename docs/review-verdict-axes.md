# Review verdicts have three axes

A review answers three separate questions. For a long time it reported one
boolean, and everything downstream tried to recover the other two by reading
prose.

| Axis | Question | Values |
| --- | --- | --- |
| **H** — harness | Could the review run at all? | `pass`, `fail` |
| **R** — reproducibility | Did the repository's own contract suite agree? | `pass`, `fail`, `not_run` |
| **S** — semantics | What did the reviewing agent conclude? | `approved`, `rejected`, `invalid`, `not_evaluated` |

The canonical vocabulary and the resolution rule live in
`src/mac/review_verdict.py`.

## The four verdicts

H is evaluated first, then R, then S:

| Verdict | Means | Spends an attempt? |
| --- | --- | --- |
| `infrastructure` | H failed. Nothing about the change was read. | **No** |
| `tests_failed` | H passed, R failed. The suite judged the change. | Yes |
| `rejected` | H passed, R passed, S rejected (or produced nothing usable). | Yes |
| `approved` | Every axis clean. | n/a — the task completes |

Harness first because a broken harness makes the other two axes meaningless;
reading them anyway is the mistake this design removes. Reproducibility before
semantics because the repository's own suite is a stronger and cheaper-to-act-on
signal than an opinion — and nothing is lost by the ordering, because all three
axes are recorded on every verdict under `review_axes`.

`invalid` semantics fail **closed**, as `rejected`. So does an unrecognised
contract failure: `classify_contract_run` returns `tests_failed`, not
`infrastructure`. Infrastructure never spends an attempt, so routing "we could
not tell" there would let a genuinely broken change retry forever behind "we
could not verify it" — failing open on the one gate this exists to enforce.

## Why: task_4ce995cb

On 2026-08-13 a worker submitted a correct one-line regression test three
times. All three reviews rejected — not on the merits. Attempts 1 and 3
reported `36 failed, 84 passed, 588 errors`; attempt 2 reported a sandbox
`UnicodeEncodeError` that was fixed eleven hours later. `attempt_count` reached
3/3, the task went terminal, and the post-mortem classifier labelled it
`scope`, whose operator remediation is "decompose" — advice that was actively
wrong for a one-line change. An equivalent task filed afterwards succeeded
unchanged.

Every one of those three runs had `588 errors` in its output, which is pytest
saying *the tests never ran*. The information was there. It was thrown away at
the moment of signing, then guessed at afterwards.

## Attempt accounting

`attempt_count` increments at CLAIM time, so a run that dies on harness grounds
has already spent one before any judgement about the work exists.

The first fix decremented it back out ("refund"), gated on a classifier reading
the rejection's free text. That machinery is gone. Instead:

- `attempt_count` is never rewritten. It is the honest number of runs started,
  and it agrees with the leases it was derived from.
- Runs whose review ended in `infrastructure` are counted on task metadata
  (`review_attempt_accounting`).
- **Consumed** attempts — the number checked against `max_attempts` — are
  `attempt_count` minus that count. `consumed_attempt_count()` is the single
  helper; the claim gate, the allocator snapshot, and the review transition all
  use it.

So attempt consumption is a property of the verdict, decided where the verdict
is produced, rather than a correction applied afterwards by a classifier.

## Producers

- `run_deterministic_review_verdict` (`src/mac/executor_finalizer.py`) — the
  reviewer-side finalizer. Missing/mismatched executor commit, failed
  bootstrap, unavailable CodeGraph, and collection errors all set H=fail.
- `_run_hub_review_verification` (`src/mac/services.py`) — the hub contract
  verifier. It never reads the diff, so it has no semantic axis and never signs
  `rejected`.

Both write a `review_axes` block onto the signed manifest.
`ReviewVerdictValidator` refuses a manifest whose axes contradict its own
verdict: the axes are load-bearing, not decoration.

## Consumers

`REVIEW_STATUS_FOR_VERDICT` in `src/mac/services.py` is the one place a verdict
becomes a `ReviewStatus`. `tests_failed` and `infrastructure` are first-class
review statuses and get their own colours in Observe — serious and warning
respectively, distinct from the critical red of a semantic `rejected`.

An `infrastructure` verdict deliberately records *nothing* about the work: no
`review_feedback` block on the task, no project failure lesson, no reviewer
summary or findings carried onto the manifest. A "lesson" distilled from a
sandbox transport fault is noise the next executor has to ignore.
