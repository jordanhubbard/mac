# Investigation: dream finding `dreamrepair:5404b15fffa355d739c21e138c5cc122` (skill subsystem)

**Task**: Establish the ground-truth state of the `mac` skill subsystem so the
low-confidence dream-cycle finding can be assessed. No behavior changes.
**Finding**: kind `failure_pattern`, scope `project`, fingerprint
`dreamrepair:5404b15fffa355d739c21e138c5cc122`, confidence `low`, backed by a
single evidence record.
**Sole evidence record**: `mem_7d29b6dcefb740bfa647da2aa4325023`
(`deployment_learning:mac`) — itself a prior *failure to repair* a
low-confidence skill finding for parent task
`task_ad368daa48f14b0cbb82795ff833ed5b`.
**Repo areas mapped**: `src/mac/skill_auto_repair.py`,
`src/mac/dream_repair_tasks.py`, `src/mac/dream_scanner.py`,
`src/mac/nap_consolidator.py`, and skill modules under `src/mac/_hermes/`
(`agent/skill_commands.py`, `agent/skill_preprocessing.py`,
`tools/skills_guard.py`, `tools/skills_hub.py`, `tools/skills_sync.py`).
**Investigated by**: fleet worker (investigation only; no production code edits).

## Status: NOT ACTIONABLE as a skill-subsystem defect

There is no reproducible skill-subsystem defect behind this finding. The
skill-focused test suites all pass on the current tree, and the finding's shape
is fully explained by the dream/nap consolidation heuristics classifying a
single self-referential "repair failed" memory as a low-confidence
`failure_pattern`. The prior repair attempts recorded against this task line
failed at coding-agent sandbox *preflight* (`probe_failed`, `executor_failed`),
which is executor/infrastructure behavior, not a skill defect.

## Ground Truth Observed

Measured in the task-owned worktree with the bootstrapped `.venv`
(`python3` 3.12.13; `git`/`gh` present; pytest/coverage installed by
`python3 scripts/bootstrap-project.py`). All skill sources were read, not
modified.

### Skill-focused tests: all green

Run via `.venv/bin/pytest`:

| Test file | Result |
| --- | --- |
| `tests/test_skill_auto_repair.py` | 30 passed |
| `tests/test_fleet_skills.py` | 2 passed |
| `tests/test_hermes_skills_dedup.py` | 14 passed |
| **Total** | **46 passed, 0 failed** |

The vendored Hermes snapshot is present (`hermes_vendor.is_vendored()` is
`True`), so `tests/test_hermes_skills_dedup.py` executed for real (the
`skipif` guard did not skip) — its 14 assertions genuinely ran and passed. No
skill test currently fails.

### Component mapping vs. the fingerprint

The fingerprint is produced by `repair_fingerprint()` in
`src/mac/dream_repair_tasks.py`, hashing normalized `{kind, scope, project,
signature, summary, affected}` of a dream candidate. It is a *dedupe key for a
dream candidate*, not a pointer into any single skill module. The candidate it
keys is `kind=failure_pattern, scope=project` — the generic bucket the nap
consolidator assigns, not a specific defect location.

- `src/mac/nap_consolidator.py` `_dream_kind()` returns `failure_pattern` for
  any record whose text contains `failure` / `failed` / `error`. The sole
  evidence record is a prior repair *failure*, so its text trivially matches and
  is bucketed as `failure_pattern`.
- `src/mac/nap_consolidator.py` `_confidence_for_records()` returns `low` for a
  single supporting record (support < 2). With exactly one evidence record, the
  finding is necessarily `low` confidence.
- Together these explain the finding's kind/scope/confidence entirely from the
  consolidation heuristics acting on one self-referential memory — no signal
  that a skill module misbehaves.

### Skill-subsystem role reference (read-only)

- `src/mac/skill_auto_repair.py`: guarded staging path for *high-confidence*
  dream skill findings (allowlist to `skills/` + `deploy/skills/`, evidence
  gate, secret/identity scrubber, fleet-generic constraint). It only engages at
  `overall_confidence=high`; a `low`-confidence finding never reaches it. It is
  currently exercised only by `tests/test_skill_auto_repair.py` and is not yet
  wired into a live pipeline call site (no non-test importers found).
- `src/mac/_hermes/agent/skill_commands.py`,
  `src/mac/_hermes/agent/skill_preprocessing.py`: in-agent skill command surface
  and preprocessing (behavioral, unrelated to dream repair).
- `src/mac/_hermes/tools/skills_guard.py`, `tools/skills_hub.py`,
  `tools/skills_sync.py`: skill guard/hub/sync tooling.
  `remove_shadowed_top_level_duplicates()` in `skills_sync.py` (the
  "Ambiguous skill name" dedup fix) is covered and green in
  `tests/test_hermes_skills_dedup.py`.

## Real defect vs. infrastructure failure

- **(a) Real reproducible skill-subsystem defect** — none observed. All 46
  skill-focused tests pass; the guard/dedup/auto-repair paths behave as
  specified.
- **(b) Executor/preflight infrastructure failure** — the prior attempts on
  this task failed at coding-agent sandbox *preflight* (`probe_failed`,
  `executor_failed`), i.e. the investigation never started, not that a skill
  test or skill guard failed. This is an executor/harness condition, outside the
  skill subsystem, and is not evidence of a skill defect.

## Evidence-gap assessment

The finding rests on a single, `low`-confidence, self-referential record: the
sole evidence is a prior *failure to repair* a low-confidence skill finding, so
the "pattern" is one report about a repair not landing rather than an observed
skill malfunction. There is no independent corroboration, no failing test, and
no reproducer. Per the consolidation heuristics above, this is the expected
low-confidence artifact of one "repair failed" memory, not a validated defect.

## Determination

- Do **not** open a skill-behavior repair from this finding: it is not
  actionable as a skill-subsystem defect.
- The finding should be treated as **low-confidence / unsubstantiated** and can
  be aged out or superseded once the executor-preflight failures on the task
  line are resolved.
- If any follow-up is desired, it belongs to the executor/preflight layer
  (fixing coding-agent sandbox `probe_failed` / `executor_failed`), not to the
  skill modules.

## Reproduction

```
python3 scripts/bootstrap-project.py
.venv/bin/pytest tests/test_skill_auto_repair.py tests/test_fleet_skills.py tests/test_hermes_skills_dedup.py -q
# => 46 passed
```
