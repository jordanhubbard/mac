!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Close-Out: low-confidence dream finding `skill` (parent `task_9c83aa5b`) — CLOSE, NOT ACTIONABLE

**Task**: Produce the final confirmed disposition / close-out for the generic
`skill` dream finding of parent `task_89a497ca1b1a46ef96bf13258660b9e0`
(repair-prerequisite `task_repair_8c81157ac9eb538d6fddf16a`). Review-only: no
edits to `src/mac/`, `src/mac/_hermes/`, `skills/`, or `deploy/skills/`.
**Parent task**: `task_9c83aa5b4d71428da6093f82b4bb4fcc` ("Confirm disposition
and close low-confidence 'skill' dream finding (not actionable)").
**Companion ground truth**:
`archive/field-notes/investigation-dream-skill-generic-area-bucket.md`
(end-to-end trace) and
`archive/field-notes/disposition-task-9c83aa5b-skill.md` (disposition), plus the
prior same-class chain
(`archive/field-notes/investigation-dream-skill-tool_or_skill_name-actionability.md`,
`archive/field-notes/investigation-dreamrepair-c8dd8037-skill.md`,
`archive/field-notes/disposition-dreamrepair-c8dd8037-skill.md`,
`archive/field-notes/closeout-dreamrepair-5404b15-skill.md`).
**Prepared by**: fleet worker (close-out node; no production code, skill, or tool
edits).

## Determination: CLOSE — NOT ACTIONABLE (low-confidence classifier/consolidation artifact)

The companion investigation established the ground truth, and this close-out
confirms it: the generic `skill` dream finding is **not a reproducible
skill-subsystem defect**. It is the expected output of the dream/nap
consolidation heuristics acting on a single self-referential evidence record,
and it never reaches the high-confidence-only `src/mac/skill_auto_repair.py`
path. Under the parent acceptance criteria, the correct deliverable for a
not-actionable finding is this committed close-out note — not a change to any
skill module, skill asset, or skill/tool test. No files under `src/mac/`,
`src/mac/_hermes/`, `skills/`, or `deploy/skills/` were modified.

## Root cause — confirmed

Each attribute of the finding is a heuristic artifact, not a defect pointer:

- **Kind `failure_pattern`.** `_dream_kind()` in `src/mac/nap_consolidator.py`
  buckets any joined record text containing `failure`/`failed`/`error` as
  `failure_pattern`. The sole evidence is a prior "repair failed" learning, so
  its text trivially matches.
- **Low confidence.** `_confidence_for_records()` in
  `src/mac/nap_consolidator.py` returns `("low", 0.35)` for `support < 2`. With
  exactly one supporting record the finding is necessarily low confidence.
- **Generic `skill` area bucket.** `_affected_labels()` in
  `src/mac/dream_repair_tasks.py` surfaces the classifier's `skill` area
  (`src/mac/dream_cycle_classifier.py`, bare `\bskill[s]?\b` word-match in
  `_SKILL_PATTERNS`) as `affected["skills"] == ["skill"]` — the literal area
  word, carrying no concrete skill/tool/provider/repo_area.
- **Fingerprint is a dedupe key, not a defect pointer.** `repair_fingerprint()`
  in `src/mac/dream_repair_tasks.py` hashes normalized `{kind, scope, project,
  signature, summary, affected}`; it keys the generic bucket for dedupe, not any
  skill module or line.
- **Single self-referential record.** The one supporting record is a prior
  no-change/failed-repair conclusion on the same class of finding, so it
  corroborates only itself and cannot independently attest to a new defect.
- **Auto-repair never engages.** `src/mac/skill_auto_repair.py` stages patches
  only at `overall_confidence == "high"` behind its allowlist/evidence/identity
  guards; a `low`-confidence finding never reaches it, so there is no guarded
  path to exercise or question here.

## Close reason

- **No reproducible defect.** The finding attaches no failing assertion,
  traceback, exit status, or provider error — by design; the scanner emits the
  skill name as an inventory-only "a skill was mentioned" signal.
- **Fully explained by heuristics.** Kind, confidence, and label are all forced
  by one self-referential record, per the confirmed root-cause chain above.
- **Correct handling path already ran.**
  `file_low_confidence_repair_tasks()` filed a review follow-up (this
  close-out) for a `low` finding rather than auto-editing.

## Evidence gap — what would be needed to raise confidence

This is a close, not a permanent verdict. The finding stays not actionable as a
skill defect absent **independent, reproducible** evidence.

## Reopen criteria

Treat the finding as actionable against a skill module only if at least one of
the following is observed and attached as evidence:

- **A failing reproducer / contract test** — a new red test under `tests/`
  (e.g. `tests/test_skill_auto_repair.py`, `tests/test_fleet_skills.py`,
  `tests/test_hermes_skills_dedup.py`, or an adjacent skill/dream suite)
  demonstrating a skill guard, dedup, or auto-repair path behaving contrary to
  spec, reproducible via `scripts/run-contract-tests.sh`.
- **A concrete non-placeholder defect location** replacing the generic `skill`
  label — an actual `repo_area`, tool, provider, or checked-in skill asset
  naming the misbehaving module.
- **Independent corroboration raising support to `>= 2`** from records that are
  not a prior investigation's own conclusion, so consolidation confidence can
  reach `medium`/`high` on its own merits.

Absent the above, the finding remains low-confidence/unsubstantiated and can be
aged out or superseded. The durable, non-skill takeaway is that self-referential
"prior no-change conclusion" memories should not seed new low-confidence
findings.

## Verification

Re-confirmed in the task-owned worktree with the bootstrapped `.venv`
(`python3` 3.12.13; all sources read, none modified):

```
python3 scripts/bootstrap-project.py
.venv/bin/pytest tests/test_skill_auto_repair.py tests/test_fleet_skills.py \
  tests/test_hermes_skills_dedup.py tests/test_dream_repair_tasks.py \
  tests/test_dream_cycle_classifier.py tests/test_nap_consolidator.py -q
# => 142 passed
```

The vendored Hermes snapshot is present, so `test_hermes_skills_dedup.py` runs
for real (its `skipif` guard does not skip). The skill suites and the
consolidation heuristics that produced the finding both pass on the current
tree; there is no failing skill test, guard, or reproducer to repair.

## Assumptions

- This is a `repo_change` task whose upstream determination is CLOSE — NOT
  ACTIONABLE, so the tracked deliverable is this close-out note rather than a
  code repair. Recorded here so the close-out outcome is auditable from
  repository history.
- Canonical synchronization, final tests/CodeGraph, commits of tracked
  modifications, and publication are owned by the deterministic host finalizer;
  this note is self-contained and unaffected by upstream drift.
