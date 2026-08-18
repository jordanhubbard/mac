!!! warning "Historical field note"
    This record preserves ground-truth investigation evidence for a single dream
    finding. It is not a current operating contract; use the numbered book and
    current runbooks for instructions.

# Ground Truth: dream finding `dreamrepair:965c6e89c762d29f07df25aafd3ac96f` (openclaw_fleet_rollout deliverable)

**Task**: Establish the ground truth for a low-confidence, project-scope dream
finding scoped to the `openclaw_fleet_rollout` deliverable for project `mac`:
determine whether a live, reproducible defect exists or whether the finding is
an evidence-volume artifact, and record the evidence gap.
**Finding**: kind `failure_pattern`, scope `project`, repo_area `mac`,
fingerprint `dreamrepair:965c6e89c762d29f07df25aafd3ac96f`, confidence `low`
(score 0.35), backed by exactly one evidence record.
**Evidence record**: `mem_4ad6f3bf11fa4534a1569edec925198b`
(record_type `deployment_learning:mac`), origin task
`task_37e658cac7d643aebbed4742a25777a2`, whose recap concerns the
`openclaw_fleet_rollout` deliverable audit.
**Prepared by**: fleet worker (investigation node; no production code, test,
skill, or deploy edits).

## Verdict: NO LIVE REPRODUCIBLE DEFECT — evidence-volume artifact, not actionable

The deliverable is present and intact, its dedicated suite is green under the
canonical hermetic runner, and the single supporting record carries no failing
assertion, stack trace, current reproduction, or named offending code path. The
0.35 confidence is the classifier's deterministic single-record structural
floor, i.e. an evidence-volume signal, not a confirmed defect. This adopts and
independently re-verifies the same ground truth reached for the near-duplicate
finding `dreamrepair:3dc2cf...` (closed NOT ACTIONABLE in
`docs/archive/field-notes/closeout-dreamrepair-3dc2cf-openclaw-fleet-rollout.md`).

No files under `src/mac/`, `tests/`, `skills/`, or `deploy/` were modified.

## Deliverable re-verified present and intact

`src/mac/openclaw_fleet_rollout.py` exposes every required public symbol, and
all import cleanly against the bootstrapped `.venv`:

- `ROLLOUT_PLAN_SCHEMA = "mac.openclaw_fleet_rollout.v1"`
  (`src/mac/openclaw_fleet_rollout.py:22`)
- `RolloutPlanStep` (`src/mac/openclaw_fleet_rollout.py:31`)
- `RolloutPlan` (`src/mac/openclaw_fleet_rollout.py:41`)
- `RolloutResult` (`src/mac/openclaw_fleet_rollout.py:65`)
- `build_staged_rollout_plan` (`src/mac/openclaw_fleet_rollout.py:83`)
- `execute_staged_rollout` (`src/mac/openclaw_fleet_rollout.py:139`)

## Canonical hermetic runner result

Command (target suite only):

    scripts/run-contract-tests.sh tests/test_openclaw_fleet_rollout.py -q

Result: **52 passed** (exit code **0**). The suite
(`tests/test_openclaw_fleet_rollout.py:1`) covers builder validation,
canary/promote staging, the executor simulate/deploy/health paths, and a
module-contract regression guard. There is no failing test, assertion, trace,
or reproducer to repair.

## Evidence-record analysis and the evidence gap

- **Single, non-reproducing record.** The only supporting evidence
  (`mem_4ad6f3bf11fa4534a1569edec925198b`, `deployment_learning:mac`) is a
  historical deployment/learning recap from an implementation-audit task
  (`task_37e658cac7d643aebbed4742a25777a2`). It carries no failing assertion,
  stack trace, current reproduction, or named offending code path. The record
  id does not appear anywhere in the checked-out repository.
- **No independent corroboration.** Nothing in the repository — neither the
  module, the 52-test suite, nor the hermetic runner — reproduces a rollout
  failure.
- **Score is a structural floor, not a severity signal.** The 0.35 confidence
  is the classifier's deterministic single-record floor
  (`CONFIDENCE_THRESHOLDS["low"] = ("low", 0.35)`,
  `src/mac/dream_cycle_classifier.py:86`), returned by `_confidence_for(...)`
  whenever `evidence_count < 2` and the candidate is not high/medium
  (`src/mac/dream_cycle_classifier.py:233`). Low confidence here reflects
  evidence volume (one record), not defect severity.

## Follow-up recommendation

- **Close the finding as NOT ACTIONABLE.** No source, skill, or tool change is
  warranted; the deliverable and its tests are intact and green.
- **No new test/guard is added.** The module-contract regression guard already
  present in `tests/test_openclaw_fleet_rollout.py` is the appropriate standing
  guard for this deliverable; adding another would be redundant.
- **Systemic note (no code change):** single-record `deployment_learning`
  recaps of implementation/audit tasks structurally surface at the 0.35 floor
  and should be triaged as historical learning artifacts rather than live
  defects unless a second, independent reproducing record is attached. This
  mirrors the systemic observation recorded for the near-duplicate `3dc2cf`
  closeout and is a triage observation, not a classifier change.
