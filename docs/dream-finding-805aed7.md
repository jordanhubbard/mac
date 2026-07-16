# Dream-Finding Assessment: dreamrepair:805aed758e12f0f95cf0c3dbf39811ce

**Task**: Establish ground truth for a low-confidence dream-cycle repair
finding scoped to repo area `mac` and the deliverable
`src/mac/openclaw_fleet_rollout.py`.
**Parent task**: task_c47ae842444e4869ae9f9fa14003e5cb
(goal: "Investigate low-confidence dream finding: mac").
**Finding fingerprint**: `dreamrepair:805aed758e12f0f95cf0c3dbf39811ce`
**Confidence**: low (score 0.35)
**Evidence record**: mem_993c40ea245548c69bcaf0bbb21971f0 (single record)
**Assessed by**: fleet worker (dream-finding review; no source/test edits)
**Assessment date**: 2026-07-16

## Status: NOT ACTIONABLE — single low-confidence record, green tests, no reproducer

The finding is a single-evidence, **low**-confidence (0.35) dream-repair
candidate. Investigation against the checked-out source confirms the target
module `src/mac/openclaw_fleet_rollout.py` behaves exactly as documented, all
52 module contract tests pass, the full contract suite is green, and the module
is a self-contained deliverable with no runtime consumers. There is no failing
behavior, no reproducer, and no concrete defect location to repair. No source
or test should be changed on the strength of this finding.

## Ground Truth Observed

Measured in the task-owned worktree with the bootstrapped `.venv`
(`python3` 3.12; `git`/`gh` present; pytest/coverage installed by
`python3 scripts/bootstrap-project.py`). The module and its tests were read,
never modified.

### (b) Module contract tests: all green

`.venv/bin/python -m pytest tests/test_openclaw_fleet_rollout.py -q`
=> **52 passed, 0 failed** (0.08s).

The 52 tests exhaustively cover the documented behavior:

- Data types: `RolloutPlanStep` fields; `RolloutPlan.canary_steps` /
  `promote_steps` filtering; `step_for_node` hit/miss; version storage;
  `RolloutResult.ok` true/false and empty default lists.
- `build_staged_rollout_plan` validation: empty/whitespace version, empty
  targets, `canary_count` below 1 or above `len(targets)`, missing/None
  `node_id` and `host`; and construction paths for single node, default canary
  count, `canary_count=2`, all-canary, field preservation, all-planned status,
  and version whitespace stripping.
- `execute_staged_rollout`: simulate mode (all succeed, `deploy_fn`/`health_fn`
  never called, statuses set to succeeded, version propagated); canary deploy
  failure halts and skips the rest; promote deploy failure halts remaining
  promote steps; failed/skipped step statuses; canary health failure halts and
  skips promote (including multi-canary early halt); `health_fn` not called for
  promote steps and called once per canary; full-success paths; canary-only
  plans; mixed success; and result-list accumulation ordering.
- `test_module_exposes_required_public_symbols` pins the public surface.

### Full contract suite: green

`scripts/run-contract-tests.sh`
=> **6663 passed, 21 skipped, 0 failed** (~8m45s), coverage safety floors met
(statements 92.76% >= 90%, branches 84.30% >= 80%).

## (a) Canary/Promote Semantics vs. the Documentation

Verified by reading `src/mac/openclaw_fleet_rollout.py` against its own module
and function docstrings. Behavior matches the documented contract exactly:

- `build_staged_rollout_plan(version, targets, *, canary_count=1)` marks the
  first `canary_count` ordered targets as `stage="canary"` and the remainder as
  `stage="promote"`, with every step starting `status="planned"`. It validates
  the version, non-empty targets, `canary_count` range (>=1 and <=len), and
  required `node_id`/`host` keys, raising `ValueError` as documented.
- `execute_staged_rollout(plan, *, deploy_fn, health_fn=None, simulate=False)`
  walks steps in order. Canary phase: each canary is deployed; on success and
  when `health_fn` is provided, the health check gates promotion — a failing
  deploy or health check marks the step `failed`, halts, and marks all
  remaining steps `skipped`. Promote phase is entered only after every canary
  succeeds; a promote deploy failure halts and skips the remaining promote
  steps. `health_fn` is only consulted for canary steps. `simulate=True` marks
  every step `succeeded` without calling `deploy_fn` or `health_fn`.
- `RolloutResult.ok` is `True` iff `failed` is empty; the succeeded/failed/
  skipped lists accumulate `node_id`s in traversal order.

All of the above is directly asserted by the passing test suite.

## (c) Consumers — self-contained deliverable

A repo-wide search for imports or references to the module and its public
symbols finds **no runtime consumers**:

- The only file importing the module is its own test,
  `tests/test_openclaw_fleet_rollout.py`.
- The two other mentions are documentation comments only, naming the module as
  a schema-convention exemplar, with no import or call:
  `src/mac/openclaw_delivery_continuity.py` and `src/mac/fleet_node_install.py`.

The module exports `ROLLOUT_PLAN_SCHEMA = "mac.openclaw_fleet_rollout.v1"` and
is a self-contained deliverable pinned by tests, not yet wired into a live
pipeline call site.

## Why the Finding Is Low-Confidence (heuristic explanation)

The fingerprint is produced by `repair_fingerprint()` in
`src/mac/dream_repair_tasks.py`, a dedupe key over a dream candidate's
normalized `{kind, scope, project, signature, summary, affected}`. Its kind and
confidence follow directly from the consolidation heuristics acting on the one
supporting memory:

- `_confidence_for_records()` in `src/mac/nap_consolidator.py` returns `low`
  for a single supporting record (support < 2). With exactly one evidence
  record, the finding is necessarily low confidence (0.35 floor).
- `_dream_kind()` buckets any record whose text contains
  `failure`/`failed`/`error` as `failure_pattern`. A prior recap mentioning a
  failed attempt trivially matches, so the generic `failure_pattern` bucket is
  assigned — not a specific defect location.

Together these explain the finding's shape entirely from one weak,
self-referential memory, with no signal that the module misbehaves.

## Evidence-Gap Assessment

The finding rests on a single, low-confidence (0.35) record with no independent
corroboration, no failing assertion, no stack trace, and no reproducer. The
checked-out module is correct and fully covered by 52 passing tests, the whole
contract suite is green, and the module has no consumers to break. This is the
expected low-confidence artifact of one record, not a validated defect.

## Determination

- **Not actionable.** Do not open a source/test repair from this finding.
- Treat the finding as **low-confidence / unsubstantiated**; it can be aged out
  or superseded.
- If corroborating evidence (a real failing scenario or reproducer against the
  documented canary/promote semantics) later appears, re-open with that
  concrete signal attached.

## Reproduction

```
python3 scripts/bootstrap-project.py
.venv/bin/python -m pytest tests/test_openclaw_fleet_rollout.py -q
# => 52 passed
scripts/run-contract-tests.sh
# => 6663 passed, 21 skipped, 0 failed
```
