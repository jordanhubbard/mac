!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Disposition: low-confidence dream finding `skill` (parent `task_9c83aa5b`) — not actionable

**Parent task**: `task_9c83aa5b4d71428da6093f82b4bb4fcc` ("Confirm disposition
and close low-confidence 'skill' dream finding (not actionable)"). The parent
deliverable was absent from the worktree (working tree clean), so ground truth
is re-established and recorded here.
**Finding**: a low-confidence `skill` dream finding surfaced by the
dream/nap consolidation heuristics — `src/mac/dream_scanner.py` (name
inventory), classified `low` by `src/mac/dream_cycle_classifier.py`, and filed
as a review follow-up by `src/mac/dream_repair_tasks.py`
(`file_low_confidence_repair_tasks`). Its only concrete label is the generic
`skill` area bucket, with no concrete `repo_area`, tool, or provider.
**Companion records**: the prior skill-finding chain already closed the same
class of finding — `archive/field-notes/investigation-dream-skill-tool_or_skill_name-actionability.md`,
`archive/field-notes/investigation-dreamrepair-c8dd8037-skill.md`,
`archive/field-notes/disposition-dreamrepair-c8dd8037-skill.md`, and
`archive/field-notes/closeout-dreamrepair-5404b15-skill.md`.
**Dispositioned by**: fleet worker (disposition only; no production code, skill,
or tool edits).

## Disposition: CLOSE — NOT ACTIONABLE (low-confidence classifier artifact)

Close the finding as **not actionable** as a skill-subsystem defect. It is the
expected output of the dream/nap consolidation heuristics acting on an
inventory-only "a skill name was mentioned" signal, not evidence that any skill
module misbehaves. This is the low-confidence "case A" outcome: record the
disposition as investigation evidence and make **no** change to skills, tools,
or source. A `low`-confidence finding never reaches the `skill_auto_repair`
path, and that path's allowlist/evidence/operator guards would (correctly)
refuse it.

## Close reason

- **No reproducible defect.** The finding attaches no failure, traceback, exit
  status, or provider error — by design. The scanner emits the skill name as an
  `info`-severity name inventory, so there is nothing to reproduce or repair.
- **The shape is fully explained by heuristics, not a defect.** The `low`
  confidence is forced because the supporting evidence has `support < 2`; the
  generic `skill` label comes from `_affected_labels()` reading a
  `signature`/`dimensions` skill name that the classifier's own
  signal-detection deliberately ignores. This classifier ↔ repair-filing
  inconsistency is a known consolidation-heuristic consideration already
  documented in the companion investigation records; it locates no skill defect.
- **The sole evidence is self-referential.** The single supporting record is the
  output of prior investigation/close work on the same class of finding, so it
  corroborates only itself and cannot independently attest to a new defect.
- **The correct handling path already ran.** `file_low_confidence_repair_tasks`
  in `src/mac/dream_repair_tasks.py` files a review follow-up (this disposition)
  for a `low` finding rather than auto-editing; `src/mac/skill_auto_repair.py`
  auto-stages only at `overall_confidence=high` behind its guards. There is no
  repair to make or guard to question.

## Evidence gap — what would be needed to raise confidence

This disposition is a close, not a permanent verdict. Reopen only if
**independent, reproducible** evidence appears that a specific skill module
misbehaves. Concretely, the finding would become actionable given any of:

- **A failing contract test or reproducer** pinning a concrete defect — e.g. a
  new red test under `tests/` demonstrating a skill guard, dedup, or
  auto-repair path behaving contrary to spec, reproducible via
  `scripts/run-contract-tests.sh`.
- **A concrete defect location** replacing the placeholder `skill` label — an
  actual `repo_area`, tool, or provider naming the misbehaving module.
- **Independent corroboration** raising `evidence_count` to `>= 2` from records
  that are not a prior investigation's own conclusion, so confidence can reach
  `medium`/`high` on its own merits.

Absent the above, the finding stays low-confidence/unsubstantiated and can be
aged out or superseded. The durable, non-skill takeaway is that self-referential
"prior no-change conclusion" memories should not seed new low-confidence
findings.

## Verification

Independently re-confirmed in the task-owned worktree with the bootstrapped
`.venv` (all sources read, not modified):

```
python3 scripts/bootstrap-project.py
.venv/bin/pytest tests/test_skill_auto_repair.py tests/test_fleet_skills.py tests/test_hermes_skills_dedup.py -q
.venv/bin/pytest tests/test_dream_repair_tasks.py tests/test_dream_cycle_classifier.py tests/test_nap_consolidator.py -q
```

The skill suites and the consolidation heuristics that produced the finding both
pass on the current tree, and the docs identity guard, documentation-book, and
generated-reference checks pass with a clean, fully committed worktree.
