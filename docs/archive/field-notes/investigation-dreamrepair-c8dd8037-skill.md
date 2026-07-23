!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Investigation: dream finding `dreamrepair:c8dd80378a16692ba4e0cd5ef57f2bf1` (skill subsystem)

**Task**: Establish the ground-truth state for the low-confidence dream-cycle
finding so its actionability can be assessed from the attached evidence alone.
Review-only — no skills, tools, or source were changed.
**Finding**: kind `failure_pattern`, scope `project`, fingerprint
`dreamrepair:c8dd80378a16692ba4e0cd5ef57f2bf1`, confidence `low` (0.35),
`evidence_count=1`, affected label `skill` (generic placeholder — no concrete
`repo_area`, tool, or provider).
**Sole evidence record**: `mem_a0477f18ded34b7e9b8b21d6673160c3`
(`deployment_learning:mac`), attached to parent line
`task_1a69e4604bae4a1aa270d87ae66f9016`. The record's own text is the OUTPUT of
a prior investigation (`task_b56b24a5`) that already concluded *no genuine
skill-subsystem defect; recommend no-change / close* and that the
`skill_auto_repair.py` guards behaved as designed.
**Repo areas corroborated (read-only)**: `src/mac/skill_auto_repair.py`,
`tests/test_skill_auto_repair.py`, `src/mac/dream_repair_tasks.py`,
`src/mac/dream_cycle_classifier.py`, `src/mac/nap_consolidator.py`.
**Investigated by**: fleet worker (investigation only; no production code edits).

## Status: NOT ACTIONABLE as a skill-subsystem defect

There is no reproducible skill-subsystem defect behind this finding. The
skill-focused test suites all pass on the current tree, and the finding's shape
(kind/scope/confidence/label) is fully explained by the dream/nap consolidation
heuristics classifying a single self-referential record as a low-confidence
`failure_pattern`. The single supporting record is itself the conclusion of a
prior investigation that already recommended no change, so the "pattern" is one
report *about a prior no-change decision*, not an observed skill malfunction.

## Ground Truth Observed

Measured in the task-owned worktree with the bootstrapped `.venv`
(`python3` 3.12.7; `git` present; pytest/coverage installed by
`python3 scripts/bootstrap-project.py`). All sources were read, not modified.

### Skill-focused tests: all green

Run via `.venv/bin/pytest`:

| Test file | Result |
| --- | --- |
| `tests/test_skill_auto_repair.py` | 36 passed |
| `tests/test_fleet_skills.py` | 2 passed |
| `tests/test_hermes_skills_dedup.py` | 14 passed |
| **Total** | **52 passed, 0 failed** |

The dream/consolidation heuristics that produced the finding were also verified
green: `tests/test_dream_repair_tasks.py`, `tests/test_dream_cycle_classifier.py`,
and `tests/test_nap_consolidator.py` (90 passed, 0 failed). No relevant test
currently fails.

### Why the finding has this exact shape

The fingerprint is produced by `repair_fingerprint()` in
`src/mac/dream_repair_tasks.py`, which hashes a normalized
`{kind, scope, project, signature, summary, affected}` payload of a dream
candidate. It is a *dedupe key for a dream candidate*, not a pointer into any
single skill module. The candidate it keys is `kind=failure_pattern,
scope=project` — the generic bucket the consolidator assigns, not a specific
defect location.

- `_dream_kind()` in `src/mac/nap_consolidator.py` returns `failure_pattern`
  whenever the joined record text contains `failure`, `failed`, or `error`. The
  sole record is a prior *repair/investigation* memo whose text trivially
  matches, so it is bucketed as `failure_pattern`.
- `_confidence_for_records()` in `src/mac/nap_consolidator.py` returns
  `low` (score `0.35`) for a single supporting record (`support < 2`); `medium`
  needs 2 and `high` needs 3+. With exactly one evidence record the finding is
  necessarily `low` confidence. The same thresholds are mirrored in
  `src/mac/dream_cycle_classifier.py` (`_confidence_for` / `CONFIDENCE_THRESHOLDS`).
- The `skill` label is a generic area bucket. `_affected_labels()` in
  `src/mac/dream_repair_tasks.py` fills `skills` only from a concrete
  `area_type=="skill"` classification area, a `dimensions` entry, or a
  `signature` starting `skill:`. Here it resolves to the placeholder area with
  no concrete skill/tool/provider/repo_area name — i.e. no defect location.

Together these explain the finding's kind, scope, confidence, and label
entirely from the consolidation heuristics acting on one self-referential
memory. None of it is a signal that a skill module misbehaves.

### How low-confidence findings are meant to be handled

`src/mac/dream_repair_tasks.py` (`file_low_confidence_repair_tasks`) is the
correct handling path for a `low` finding: it files a **review** follow-up task
(the parent investigation line) rather than auto-editing anything. It gates on
`overall_confidence == "low"` and skips inventory-only candidates.

`src/mac/skill_auto_repair.py` is the only auto-staging path, and it engages
**only** at `overall_confidence=high` for checked-in skill assets. Its guards —
allowlist (`skills/`, `deploy/skills/`), evidence gate, secret scrubber,
operator-identity scrubber, and the `mac.skill_auto_repair.v1` audit result —
are covered green by `tests/test_skill_auto_repair.py`. A `low`-confidence
finding never reaches this path, so there is no repair to make or guard to
question.

## Real defect vs. classifier artifact

- **(a) Real reproducible skill-subsystem defect** — none observed. All 52
  skill-focused tests pass; the guard/dedup/auto-repair paths behave as
  specified.
- **(b) Low-confidence classifier artifact** — the finding is the expected
  output of the consolidation heuristics on a single "repair/investigation"
  memory. It is self-referential (the evidence is the prior investigation's own
  conclusion), so it corroborates *only itself*.

## Evidence-gap assessment

The finding rests on a single, `low`-confidence, self-referential record:

- **Single record** — `evidence_count=1`; `medium`/`high` confidence is
  unreachable without independent corroboration.
- **Self-referential** — the sole record is the OUTPUT of parent
  investigation `task_b56b24a5`, which already concluded no-change/close, so it
  cannot independently attest to a new defect.
- **No reproduction** — no failing test, no reproducer, and no concrete defect
  location (the `skill` label is a placeholder, not a module/tool/provider).

## Determination

- Do **not** open a skill-behavior repair from this finding: it is not
  actionable as a skill-subsystem defect.
- Treat the finding as **low-confidence / unsubstantiated**; it can be aged out
  or superseded once no additional independent evidence accumulates.
- No follow-up belongs to the skill modules. If anything, the durable takeaway
  is that self-referential "prior no-change conclusion" memories should not seed
  new low-confidence findings — a consolidation-heuristic consideration, not a
  skill defect.

## Reproduction

```
python3 scripts/bootstrap-project.py
.venv/bin/pytest tests/test_skill_auto_repair.py tests/test_fleet_skills.py tests/test_hermes_skills_dedup.py -q
# => 52 passed
.venv/bin/pytest tests/test_dream_repair_tasks.py tests/test_dream_cycle_classifier.py tests/test_nap_consolidator.py -q
# => 90 passed
```
