!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Disposition: dream finding `dreamrepair:c8dd80378a16692ba4e0cd5ef57f2bf1` (skill subsystem)

**Task**: Produce the final disposition of the low-confidence skill dream
finding, building on the companion ground-truth memo. Review-only — no skills,
tools, or source were changed.
**Finding**: kind `failure_pattern`, scope `project`, fingerprint
`dreamrepair:c8dd80378a16692ba4e0cd5ef57f2bf1`, confidence `low` (0.35),
`evidence_count=1`, affected label `skill` (generic placeholder — no concrete
`repo_area`, tool, or provider).
**Companion memo**: the ground-truth investigation record
`archive/field-notes/investigation-dreamrepair-c8dd8037-skill.md`, which
established from the attached evidence alone that this is not a skill-subsystem
defect.
**Dispositioned by**: fleet worker (disposition only; no production code edits).

## Disposition: CLOSE — NOT ACTIONABLE (low-confidence classifier artifact)

Close the finding as **not actionable** as a skill-subsystem defect. It is the
expected output of the dream/nap consolidation heuristics acting on a single,
self-referential memory, not a signal that any skill module misbehaves. This is
the low-confidence "case A" outcome: record the disposition as investigation
evidence and make **no** change to skills, tools, or source. In particular, do
not force a `skill_auto_repair` edit — a `low`-confidence finding never reaches
that path, and its guards would (correctly) refuse it.

## Close reason

- **No reproducible defect.** The skill-focused suites all pass on the current
  tree (`tests/test_skill_auto_repair.py`, `tests/test_fleet_skills.py`,
  `tests/test_hermes_skills_dedup.py` — 52 passed), as do the consolidation
  heuristics that produced the finding (`tests/test_dream_repair_tasks.py`,
  `tests/test_dream_cycle_classifier.py`, `tests/test_nap_consolidator.py` —
  90 passed). Nothing currently fails.
- **The finding's shape is fully explained by heuristics, not a defect.** The
  fingerprint is a dedupe key for a dream candidate produced by
  `repair_fingerprint()` in `src/mac/dream_repair_tasks.py`; the
  `failure_pattern` kind comes from `_dream_kind()` in
  `src/mac/nap_consolidator.py` matching the words `failure`/`failed`/`error`
  in the record text; the `low` confidence is forced by
  `_confidence_for_records()` because `support < 2`; and the `skill` label is a
  generic area bucket from `_affected_labels()` with no concrete
  skill/tool/provider/repo_area. None of these locate a defect.
- **The sole evidence is self-referential.** The one supporting record is the
  OUTPUT of a prior investigation that already concluded no-change/close, so it
  corroborates only itself — it cannot independently attest to a new defect.
- **The correct handling path already ran.** `file_low_confidence_repair_tasks`
  in `src/mac/dream_repair_tasks.py` files a review follow-up (this line) for a
  `low` finding rather than auto-editing; `src/mac/skill_auto_repair.py`
  auto-stages only at `overall_confidence=high` behind allowlist, evidence,
  secret, and operator-identity guards. There is no repair to make or guard to
  question.

## Evidence gap — what would be needed to raise confidence

This disposition is a close, not a permanent verdict. Reopen only if
**independent, reproducible** evidence appears that a specific skill module
misbehaves. Concretely, the finding would become actionable given any of:

- **A failing contract test or reproducer** that pins a concrete defect — e.g.
  a new red test under `tests/` demonstrating a skill guard, dedup, or
  auto-repair path behaving contrary to spec, reproducible via
  `scripts/run-contract-tests.sh`.
- **A concrete defect location** replacing the placeholder `skill` label — an
  actual `repo_area`, tool, or provider naming the misbehaving module.
- **Independent corroboration** raising `evidence_count` to `>= 2` from records
  that are not the prior investigation's own conclusion, so confidence can
  reach `medium`/`high` on its own merits.

Absent the above, the finding stays low-confidence/unsubstantiated and can be
aged out or superseded. The durable, non-skill takeaway (a consolidation
heuristic consideration, not a skill defect) is that self-referential "prior
no-change conclusion" memories should not seed new low-confidence findings.

## Verification

Independently re-confirmed in the task-owned worktree with the bootstrapped
`.venv` (all sources read, not modified):

```
python3 scripts/bootstrap-project.py
.venv/bin/pytest tests/test_skill_auto_repair.py tests/test_fleet_skills.py tests/test_hermes_skills_dedup.py -q
# => 52 passed
.venv/bin/pytest tests/test_dream_repair_tasks.py tests/test_dream_cycle_classifier.py tests/test_nap_consolidator.py -q
# => 90 passed
```
