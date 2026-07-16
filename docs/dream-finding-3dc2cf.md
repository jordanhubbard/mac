# Dream-Finding Assessment: dreamrepair:3dc2cf317ea21e032952a355c3550f88

**Task**: Establish ground truth for a low-confidence dream-cycle repair
finding scoped to the OpenClaw fleet-rollout deliverable for project `mac`.
**Parent task**: task_5287b80a718c432ca0e78807ccf45911
(goal: "Investigate low-confidence dream finding: mac").
**Finding fingerprint**: `dreamrepair:3dc2cf317ea21e032952a355c3550f88`
**Finding kind / scope**: `failure_pattern`, scope `project`, repo_area `mac`.
**Confidence**: low (score 0.35), backed by exactly one evidence record.
**Origin task of the one evidence record**: task_663c141c19494c76b0a0d28caa8fa07a
**Evidence record**: mem_a718733f11c040e98ae608ad93307df6
(record_type `deployment_learning:mac`)
**Assessed by**: fleet worker (dream-finding review; no source or test edits)
**Assessment date**: 2026-07-16

## Status: DOES NOT REPRODUCE — historical learning record, not a live defect

The finding is a **failure_pattern** with **project** scope and **low**
confidence (score 0.35), backed by exactly **one** `deployment_learning:mac`
evidence record. Investigation confirms the referenced deliverable and its test
suite are intact, committed in the baseline, and fully green. The finding is a
second-order artifact of a *prior* implementation/repair learning record, not
evidence of a current defect in the checked-in `openclaw_fleet_rollout` module.
No source or test should be changed on the strength of this finding.

## Ground Truth: Deliverable Is Intact

- `src/mac/openclaw_fleet_rollout.py:1` exists (209 lines) and exposes every
  required public symbol:
  - `RolloutPlanStep` — `src/mac/openclaw_fleet_rollout.py:31`
  - `RolloutPlan` — `src/mac/openclaw_fleet_rollout.py:41`
  - `RolloutResult` — `src/mac/openclaw_fleet_rollout.py:65`
  - `build_staged_rollout_plan` — `src/mac/openclaw_fleet_rollout.py:83`
  - `execute_staged_rollout` — `src/mac/openclaw_fleet_rollout.py:139`
  - Plan-schema identifier `ROLLOUT_PLAN_SCHEMA = "mac.openclaw_fleet_rollout.v1"`
    — `src/mac/openclaw_fleet_rollout.py:23`
- Both files are committed in the baseline commit
  `a77007875519a49e9a089af0db8bc0589d1002b7` ("MAC OpenShell sandbox baseline");
  `git log` on each path shows no post-baseline modification. The working tree
  is clean.

## Ground Truth: Test Suite Covers the Contract

`tests/test_openclaw_fleet_rollout.py:1` (545 lines, 52 tests) covers all
areas the finding would implicate:

- **Builder validation** — empty/whitespace version, empty targets,
  `canary_count < 1`, `canary_count > len(targets)`, missing/None `node_id`,
  missing/None `host` (`tests/test_openclaw_fleet_rollout.py:121`).
- **Canary/promote staging** — single-node canary, default two-node split,
  `canary_count=2`, all-canary when count equals length, stage assignment and
  version stripping (`tests/test_openclaw_fleet_rollout.py:175`).
- **Executor simulate path** — all-succeed with no `deploy_fn`, `deploy_fn`
  and `health_fn` never called, statuses set to succeeded, version propagated
  (`tests/test_openclaw_fleet_rollout.py:224`).
- **Executor deploy path** — canary/promote deploy failure halts and skips the
  remainder, failed/skipped statuses recorded
  (`tests/test_openclaw_fleet_rollout.py:272`).
- **Executor health path** — canary health failure halts and skips promote,
  `health_fn` only called for canary steps, called once per canary step
  (`tests/test_openclaw_fleet_rollout.py:313`).
- **Module-contract regression guard** — asserts the five required public
  symbols exist and have the right kinds
  (`tests/test_openclaw_fleet_rollout.py:522`).

## Test Results (After Bootstrap)

Environment bootstrapped with `python3 scripts/bootstrap-project.py`
(editable install of `mac==0.1.0` succeeded).

- Module suite, direct pytest:
  `.venv/bin/python -m pytest tests/test_openclaw_fleet_rollout.py -q`
  → `52 passed in 0.08s` (exit 0).
- Module suite via canonical hermetic runner:
  `scripts/run-contract-tests.sh tests/test_openclaw_fleet_rollout.py -q`
  → `52 passed in 0.04s` (exit 0).

No failing assertion, error, or regression reproduces. The `failure_pattern`
does not manifest against the checked-out code.

## What the Single Evidence Record Represents

The lone backing record `mem_a718733f11c040e98ae608ad93307df6` is a
`deployment_learning:mac` recap from the original implementation/repair task
`task_663c141c19494c76b0a0d28caa8fa07a`. It captures a *historical* build/repair
learning about the rollout deliverable — not a live, reproducible defect. It
contains no failing assertion, stack trace, current reproduction, or named
offending code path. It is therefore an original-implementation "failure/repair"
learning artifact, not a live defect signal.

## Why Confidence Is Low (0.35)

The 0.35 score is the classifier's structural floor, assigned deterministically
when a finding is backed by a single evidence record:

- `docs`-level contract of the classifier states "low — Signal is present in the
  artifact text, but only a single evidence record backs it. Score ~= 0.35"
  (`src/mac/dream_cycle_classifier.py:14`).
- The threshold table sets `"low": ("low", 0.35)`
  (`src/mac/dream_cycle_classifier.py:87`).
- `_confidence_for(...)` returns the low threshold whenever
  `evidence_count < 2` and the candidate is not high/medium
  (`src/mac/dream_cycle_classifier.py:233`), matching this finding's single
  `deployment_learning:mac` record exactly.

So low confidence here is a property of the *evidence volume* (one record), not
a measure of severity or of a confirmed defect.

## Evidence Gap

- Only one evidence record exists, and it is a historical learning recap rather
  than a reproduction. There is no independent, current observation of a rollout
  failure.
- No failing test, trace, or offending code path is attached to the finding.
- The classifier floor (single record -> 0.35) is the sole driver of the low
  score; nothing in the checked-out repository corroborates a live failure.

## Conclusion

The finding `dreamrepair:3dc2cf317ea21e032952a355c3550f88` **does not correspond
to any real current defect** in the repository. The `openclaw_fleet_rollout`
deliverable and its 52-test suite are intact, committed in the baseline, and
pass cleanly under both direct pytest and the canonical hermetic contract
runner. The single low-confidence evidence record is an original-implementation
learning artifact, and the 0.35 score is the classifier's structural
single-record floor. No source or test changes are warranted.
