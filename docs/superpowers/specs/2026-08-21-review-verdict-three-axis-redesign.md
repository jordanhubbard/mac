# Review Verdict Redesign: Separate Harness, Reproducibility, and Semantics

Status: accepted decisions; implementation in progress (task_b520b9ca)
Author: investigation into systemic review-gate failures
Related: `docs/superpowers/specs/2026-05-31-autonomous-review-fix-loop-design.md`

## Summary

The review gate is failing systemically because it collapses three
independent signals into a single boolean verdict at exactly the point where
the discriminating evidence exists, and the collapse fails toward "reject and
blame the work." Everything downstream then reconstructs the lost intent from
free text.

This document proposes replacing the single `approved | rejected` verdict with
a structured, three-axis outcome so that infrastructure faults, reproducibility
failures, and semantic judgments are recorded and acted on distinctly.

The task state machine does **not** change. The verdict *contract* and the
retry-accounting *inputs* change.

## The three axes

A review answers three orthogonal questions. They are currently conflated.

| Axis | Question | Owner | Correct failure handling |
| --- | --- | --- | --- |
| **H** — harness health | Did the review infrastructure run at all? (clone, bootstrap, CodeGraph service, integration harness, sandbox) | Platform | Retry; refund attempt; **never** record a verdict about the work; leave semantic judgment untouched |
| **R** — reproducibility | Does the exact reviewed commit pass the deterministic suite? | Repo + executor | Veto approval, but record distinctly from "reviewer rejected" and from flaky/env failure |
| **S** — semantic | Does an independent reviewer approve the change? | Reviewer LLM | Real feedback; consume an attempt |

## Root cause

### The collapse

`run_deterministic_review_verdict` in `src/mac/executor_finalizer.py` computes
the final verdict as a single AND across unlike subsystems:

```python
independent_pass = (
    bootstrap_ok and tr.returncode == 0 and codegraph_audit_passed(codegraph) and integration_ok
)
...
verdict = (
    "approved"
    if semantic_valid and semantic_verdict == "approved" and independent_pass
    else "rejected"
)
```

`independent_pass` folds axis **H** (bootstrap, CodeGraph, integration harness)
together with axis **R** (the test run) into one boolean. When any of them
fails for any reason — a broken sandbox, a CodeGraph outage, a bootstrap flake,
or test-collection errors unrelated to the change — the final `verdict`
becomes `rejected`, even when the reviewer LLM returned `approved`.

The finalizer's own docstring states the invariant it means to protect:

> The review agent owns the semantic verdict. Deterministic checks may veto an
> approval, but they must never turn a semantic rejection into an approval.

The implementation honors that direction but never guards the opposite harm it
actually commits: **a harness/infrastructure failure vetoes a semantic approval
and is recorded as a rejection of the work.** There is no outcome for "the
harness under me is broken."

### The information is destroyed at the collapse

The per-subsystem results *do* exist in the manifest `checks[]` (semantic,
codegraph, cooperative_integration, review_verdict_finalizer, each with a
returncode). But the top-level `verdict: rejected` flattens them, and the
control plane's retry decision never reads `checks[]`.

### The downstream classifier is an epicycle

`ReviewService.submit_review` tries to recover the lost distinction by
classifying the rejection from free text:

```python
classification = classify_review_failure(
    reason or "",
    error=str((rejected_feedback or {}).get("feedback") or "") or None,
)
refund_attempt = bool(classification.is_infrastructure)
```

`classify_review_failure` (`src/mac/review_failure_classifier.py`) then pattern
-matches English (`hub.*contract` → infrastructure, etc.). When the feedback
text lacks a magic word it defaults to `unknown → semantic`, which **consumes an
attempt** and marches the task toward `blocked`/terminal.

This entire module and the attempt-refund logic exist only to undo the collapse
after the fact, from a lossy signal, instead of preventing it.

### Observed impact (recorded in-code)

From `src/mac/review_service.py` (task_4ce995cb, 2026-08-13): a worker submitted
a correct one-line regression test three times; all three reviews rejected with
"hub contract verification failed" carrying 588 collection errors and, on one
attempt, a sandbox `UnicodeEncodeError`. `attempt_count` reached 3/3, the task
went terminal, and the post-mortem classifier labeled it "scope → decompose" —
advice actively wrong for a one-line change. An equivalent task filed afterward
succeeded unchanged.

That is three infrastructure failures recorded as three semantic rejections of
correct work, plus a mislabeled remediation — the exact failure this redesign
removes.

## Goals

- Record H, R, and S as distinct, structured fields on review verdict evidence.
- Make the retry/attempt decision read structured checks, not free text.
- Guarantee a harness (H) failure never records a verdict about the work and
  never consumes an attempt.
- Preserve the existing invariant: deterministic checks may veto an approval;
  they may never manufacture an approval.
- Reduce `classify_review_failure` from a load-bearing guesser to, at most, a
  compatibility shim for legacy evidence.

## Non-goals

- Do not add or remove task states. `needs_review → reviewing →
  approved⇒publish / rejected⇒reopen` stays.
- Do not change reviewer independence / cross-LLM diversity requirements.
- Do not change publication (approval-only) behavior.
- Do not weaken the fail-closed rule for unknown verdicts (`_verdict_value`
  stays `unknown → rejected`).

## Proposed design

### 1. A structured outcome, not a boolean

Review verdict evidence keeps `evidence_type: review_verdict` and gains an
explicit outcome discriminator. Recommended vocabulary:

```json
{
  "verdict": "approved" | "rejected" | "infrastructure",
  "semantic_verdict": "approved" | "rejected" | "invalid",
  "reproducibility": "pass" | "fail" | "not_applicable",
  "harness": "ok" | "failed",
  "harness_failure_class": "clone|bootstrap|codegraph|integration|sandbox|...",
  "checks": [ { "name": "...", "status": "pass|fail", "returncode": 0 } ]
}
```

- `approved` requires `harness == ok` **and** `semantic_verdict == approved`
  **and** `reproducibility == pass` (for repo work). Unchanged in strictness.
- `rejected` means `harness == ok` and the *work* was judged inadequate —
  either `semantic_verdict == rejected` or `reproducibility == fail`. Feedback
  is required, as today.
- `infrastructure` is new: `harness == failed`. It is **not** a verdict about
  the work. It carries `harness_failure_class` and drives retry, never
  attempt consumption, never `blocked`.

### 2. Evaluation order: H → S → R

`run_deterministic_review_verdict` should evaluate in this order and short
-circuit:

1. **H first.** If clone/bootstrap/CodeGraph/integration/sandbox did not run
   cleanly, emit `verdict: infrastructure` with `harness_failure_class`. Do not
   compute or record a work verdict. Do not require the semantic verdict to be
   present.
2. **S next.** If the reviewer returned `rejected`, that is a real semantic
   rejection regardless of R.
3. **R last.** For repo work with `semantic_verdict == approved`, run the
   deterministic suite. `pass` → `approved`; `fail` → `rejected` with
   `reproducibility: fail` so it is distinguishable from a semantic rejection.

The key change is that bootstrap/CodeGraph/integration move **out** of
`independent_pass` and **into** the H gate, where their failure yields
`infrastructure`, not `rejected`.

### 3. Retry/attempt accounting reads structured fields

`ReviewService.submit_review` decides refund/consume from the structured
outcome, not `classify_review_failure(reason, feedback)`:

- `verdict == infrastructure` → reopen, **refund** attempt (or better: never
  consume; see open questions), reason names `harness_failure_class`.
- `verdict == rejected`, `reproducibility == fail` → reopen, consume attempt,
  feedback carries the failing check(s).
- `verdict == rejected`, `semantic_verdict == rejected` → reopen, consume
  attempt, feedback carries findings.

`classify_review_failure` is retained only to interpret legacy evidence that
predates the structured fields; new evidence must not depend on it.

### 4. Reviewer authority and the infrastructure escape hatch

The reviewer "skill" (the `review` agent prompt in
`deploy/codex-runner/mac-task-executor-opencode-review`) is largely sound and
is **not** the cause of failures — the reviewer is frequently correct and
silently overruled by H. Two additions:

- The reviewer should be able to signal "I could not review because the
  environment is broken" and have that map to `infrastructure`, rather than
  being forced into `approved | rejected`.
- The prompt line "Passing tests never require you to approve a semantic
  defect" stays; add its dual: a harness failure must not be reported as a
  defect in the work.

### 5. Secondary instance of the same anti-pattern (build side)

`deploy/codex-runner/mac-task-executor-opencode-build` treats "could not detect
test command — manual verification required" as a work failure routed to
`needs_review`. That is a *detection/harness* gap (H) encoded as a *work*
failure. It should be labeled as an infrastructure/needs-config outcome, not a
failing attempt. Fix in the same spirit, separately sequenced.

## State machine impact

None to the states themselves. The `reviewing → open|blocked` transition on
rejection is unchanged; only the *reason* it fires and whether it consumes an
attempt change. `infrastructure` reopens like a retryable failure and never
lands in `blocked` via the review path.

## Migration and backward compatibility

- Evidence validators (`ReviewVerdictValidator`, `_find_review_verdict_evidence`)
  must accept the new fields and the `infrastructure` verdict, while continuing
  to reject `needs_changes` and to fail closed on unknown verdicts.
- Old evidence without the structured fields is interpreted by the legacy
  `classify_review_failure` shim.
- `completion_authorized` / publication paths are unaffected: only `approved`
  verdicts pointing at the current executor evidence authorize completion.

## Testing plan

Unit:

1. H failure (bootstrap/codegraph/integration/clone) yields `verdict:
   infrastructure`, no work verdict, and does not consume an attempt.
2. Semantic `rejected` with clean harness yields `rejected`,
   `semantic_verdict: rejected`, consumes an attempt.
3. Semantic `approved` + `reproducibility: fail` yields `tests_failed`,
   distinguishable from case 2.
4. Semantic `approved` + clean harness + tests pass yields `approved`.
5. `submit_review` consume decision is driven by the canonical verdict.
   `classify_review_failure` is not consulted. There is no `attempt_refunded`.
6. `_verdict_value` still returns `rejected` for unknown verdicts.
7. Legacy evidence (no structured fields) still classifies via the shim.

Regression:

8. Reconstruct task_4ce995cb: 588 collection errors during review yields three
   `infrastructure` outcomes, zero attempt consumption, task never terminal.

## Resolved decisions (2026-08-21)

- **Remove the review-path refund machinery.** `classify_review_failure` must
  not drive attempt accounting. A work-quality verdict (`rejected` or
  `tests_failed`) consumes the coding attempt that claim already counted. An
  `infrastructure` verdict restores that count because no work verdict was
  recorded — operators see `work_attempt_consumed: false`, not
  `attempt_refunded`.
- **`tests_failed` is a first-class disposition.** Canonical verdicts are
  `approved | rejected | tests_failed | infrastructure`. ReviewStatus, observe
  colouring, and task-detail review chips show `tests_failed` distinctly from
  semantic `rejected`.
- Repeated infrastructure retries keep using the existing review-nudge /
  retraction caps; they do not burn `max_attempts`. A dedicated infra cap is
  follow-up, not this change.

## Recommended sequence

1. Add structured fields to the finalizer output and move H out of
   `independent_pass` into an H-first gate (`executor_finalizer.py`).
2. Extend validators/finders to accept the new fields and `infrastructure`.
3. Switch `submit_review` retry accounting to read structured fields; demote
   `classify_review_failure` to a legacy shim.
4. Add the reviewer infrastructure escape hatch to the review executor prompt.
5. Apply the same distinction to the build-side "no test command" gate.
6. Land the task_4ce995cb regression test.
