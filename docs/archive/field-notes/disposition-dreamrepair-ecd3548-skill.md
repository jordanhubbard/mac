!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Disposition: low-confidence dream finding `skill` (`dreamrepair:ecd3548120e07c38d04e46f0c62e16dd`) — CLOSE, NOT ACTIONABLE

**Task**: Apply the disposition for the low-confidence `skill` dream-repair
finding (fingerprint `dreamrepair:ecd3548120e07c38d04e46f0c62e16dd`,
kind `failure_pattern`, scope `project`, confidence `low`, 1 evidence record)
per the parent acceptance criteria, using the upstream investigation's
actionability determination and named evidence gap. Documentation-only: no
edits to `src/mac/`, `src/mac/_hermes/`, `skills/`, or `deploy/skills/`.

**Parent (disposition integration)**: `task_3903924767de421ca79033e16e657c2d`
("Investigate low-confidence dream finding: skill").
**Upstream investigation (consumed)**:
`task_c4cac023e5f44822896ebb1e678872ab` ("Investigate skill dream-repair
finding: confirm actionability and evidence gap"), completed and approved.
**Prior same-class chain (precedent)**:
`archive/field-notes/closeout-task-9c83aa5b-skill.md`,
`archive/field-notes/disposition-task-9c83aa5b-skill.md`,
`archive/field-notes/investigation-dream-skill-generic-area-bucket.md`,
`archive/field-notes/investigation-dreamrepair-c8dd8037-skill.md`,
`archive/field-notes/disposition-dreamrepair-c8dd8037-skill.md`,
`archive/field-notes/closeout-dreamrepair-5404b15-skill.md`.

## Determination: CLOSE — NOT ACTIONABLE

The upstream investigation established, and this disposition re-confirms against
the current task worktree, that the generic `skill` dream finding is **not a
reproducible skill-subsystem defect**. It is the expected output of the
dream/nap consolidation heuristics acting on a single self-referential evidence
record, and it never reaches the high-confidence-only guarded auto-repair path
in `src/mac/skill_auto_repair.py`.

Under the parent acceptance criteria, the correct deliverable for a
not-actionable finding is this committed close-out/disposition note — not a
change to any skill module, skill asset, or skill/tool test. No files under
`src/mac/`, `src/mac/_hermes/`, `skills/`, or `deploy/skills/` were modified.

## Root cause — re-verified in this worktree

Each attribute of the finding is a heuristic artifact, not a defect pointer.
Line references are to the current tree at the time of this note.

- **Kind `failure_pattern`.** `_dream_kind()` in
  `src/mac/nap_consolidator.py` lower-cases the joined record text and returns
  `"failure_pattern"` whenever it contains `failure`/`failed`/`error`. The sole
  evidence record is a prior "repair failed" learning, so its text trivially
  matches.
- **Low confidence.** `_confidence_for_records()` in
  `src/mac/nap_consolidator.py` returns `("low", 0.35)` for `support < 2`
  (`support == 2` -> `medium`, `support >= 3` -> `high`). With exactly one
  supporting record the finding is necessarily low confidence.
- **Generic `skill` area bucket.** `_affected_labels()` in
  `src/mac/dream_repair_tasks.py` surfaces the classifier's `skill` area
  (`src/mac/dream_cycle_classifier.py`, bare `\bskill[s]?\b` word-match in
  `_SKILL_PATTERNS`) as `affected["skills"] == ["skill"]` — the literal area
  word, carrying no concrete skill/tool/provider/repo_area.
- **Fingerprint is a dedupe key, not a defect pointer.** `repair_fingerprint()`
  in `src/mac/dream_repair_tasks.py` hashes normalized
  `{kind, scope, project, signature, summary, affected}` and returns
  `"dreamrepair:" + sha256(...)[:32]`. It keys the generic bucket for dedupe
  across repeated cycle reports, not any skill module or line.
- **Single self-referential record.** The one supporting record is a prior
  no-change/failed-repair conclusion on the same class of finding, so it
  corroborates only itself and cannot independently attest to a new defect.
- **Auto-repair never engages.** `file_low_confidence_repair_tasks()` in
  `src/mac/dream_repair_tasks.py` only files a review follow-up task for
  `overall_confidence == "low"` findings (it skips anything not low, anything
  inventory-only, and anything with no affected area) — it does not auto-edit.
  `src/mac/skill_auto_repair.py` stages patches only at
  `overall_confidence == "high"` behind its allowlist (`skills/` or
  `deploy/skills/` only), evidence-required, secret, and identity guards. A
  `low`-confidence finding never reaches it, so there is no guarded path to
  exercise or question here.

## Close reason

- **No reproducible defect.** The finding attaches no failing assertion,
  traceback, exit status, or provider error — by design; the scanner emits the
  skill name as an inventory-only "a skill was mentioned" signal.
- **Fully explained by heuristics.** Kind, confidence, and label are all forced
  by one self-referential record, per the re-verified root-cause chain above.
- **Correct handling path already ran.**
  `file_low_confidence_repair_tasks()` filed a review follow-up (this
  disposition/close-out) for a `low` finding rather than auto-editing.

## Evidence gap — what the investigation named

This is a close, not a permanent verdict. The finding stays not actionable as a
skill defect absent **independent, reproducible** evidence. The named gap:

- No failing reproducer or contract test ties the finding to a specific skill
  module, loader, guard, dedupe, or auto-repair behavior.
- The `skill` label is the classifier's generic area word, not a concrete
  `repo_area`/tool/provider/checked-in skill asset naming a misbehaving module.
- Support is a single self-referential record, so consolidation confidence
  cannot rise above `low` on its own merits.

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
(all sources read, none modified):

```
python3 scripts/bootstrap-project.py
scripts/run-contract-tests.sh
```

The skill suites and the consolidation heuristics that produced the finding
pass on the current tree; there is no failing skill test, guard, or reproducer
to repair.

## Assumptions

- This is a `repo_change` task whose upstream determination is CLOSE — NOT
  ACTIONABLE, so the tracked deliverable is this disposition/close-out note
  rather than a code repair. Recorded here so the outcome is auditable from
  repository history.
- Canonical synchronization, final tests/CodeGraph, commits of tracked
  modifications, and publication are owned by the deterministic host finalizer;
  this note is self-contained and unaffected by upstream drift.
