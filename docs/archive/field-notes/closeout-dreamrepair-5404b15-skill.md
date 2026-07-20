!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Close-Out: dream finding `dreamrepair:5404b15fffa355d739c21e138c5cc122` (skill subsystem)

**Task**: Satisfy the parent acceptance criteria for the low-confidence dream
finding scoped to the skill subsystem — either apply the smallest appropriate
skill repair, or, if the finding is not actionable, produce a concrete,
committed follow-up plan / close-out note.
**Parent task**: "Investigate low-confidence dream finding: skill".
**Finding**: kind `failure_pattern`, scope `project`, fingerprint
`dreamrepair:5404b15fffa355d739c21e138c5cc122`, confidence `low`, backed by a
single self-referential evidence record.
**Prepared by**: fleet worker (remediation node; no production code edits).

## Determination: NOT ACTIONABLE — no skill repair applied

The upstream investigation established the ground truth that this finding is
**not a reproducible skill-subsystem defect**
(`docs/investigation-dreamrepair-5404b15-skill.md`), and the prerequisite
investigation independently reached the same conclusion
(`docs/prereq-task-fd2f34.md`). Under the parent acceptance criteria, when the
finding is not actionable the correct deliverable is this committed close-out
note — not a change to any skill module, skill asset, or skill/tool test. No
files under `src/mac/`, `src/mac/_hermes/`, `skills/`, or `deploy/skills/` were
modified.

## Corroboration in the task worktree

The skill-focused suites cited by the investigation were re-run here against the
bootstrapped `.venv` (`python3` 3.12.13) and all pass, reproducing the
investigation's result:

```
.venv/bin/pytest tests/test_skill_auto_repair.py tests/test_fleet_skills.py \
  tests/test_hermes_skills_dedup.py -q
# => 46 passed
```

The vendored Hermes snapshot is present, so `test_hermes_skills_dedup.py` runs
for real (its `skipif` guard does not skip). There is no failing skill test,
guard, or reproducer to repair.

## Why the finding is not a skill defect (evidence gap)

- **Single self-referential record.** The only supporting evidence
  (`mem_7d29b6dcefb740bfa647da2aa4325023`, `deployment_learning:mac`) is a prior
  *failure to repair* a low-confidence skill finding — a meta-observation about
  an earlier attempt, not an observed skill malfunction (no failing assertion,
  stack trace, reproduction, or offending skill asset).
- **The kind/scope/confidence are heuristic artifacts.** In
  `src/mac/nap_consolidator.py`, `_dream_kind()` buckets any text containing
  `failure`/`failed`/`error` as `failure_pattern`, and
  `_confidence_for_records()` returns `low` whenever support is `< 2`. One
  "repair failed" memory therefore necessarily produces a `low`-confidence
  `failure_pattern` regardless of skill health.
- **The fingerprint is a dedupe key, not a defect pointer.**
  `repair_fingerprint()` in `src/mac/dream_repair_tasks.py` hashes the
  normalized dream-candidate fields (`kind, scope, project, signature, summary,
  affected`). It keys the generic `failure_pattern`/`project` bucket, not a
  specific skill module or line.
- **The real blocker is executor/preflight, not skills.** The prior attempts on
  this task line failed at coding-agent sandbox *preflight*
  (`class=probe_failed`, `executor_failed`) — the run never started. That is an
  executor/runtime-availability event outside the checked-in skill subsystem.
- **Auto-repair never engages here.** `src/mac/skill_auto_repair.py` only stages
  patches for `overall_confidence=high` findings (allowlisted to `skills/` and
  `deploy/skills/`, behind evidence and secret/identity guards). A
  `low`-confidence finding never reaches it, so there is no guarded path to
  exercise from this finding.

## Follow-up plan

1. **Age out / supersede** this finding as a low-confidence, single-record,
   self-referential artifact. No skill-source change is warranted.
2. **Re-file at the correct layer if it recurs.** If the preflight fallback
   recurs, open it against the executor/runtime-availability path
   (probe/gateway), not the skill subsystem, so future dream findings carry a
   tool/provider label rather than the bare `skill` token.
3. **Reopen criteria (what stronger evidence would be required).** Treat this
   finding as actionable against a skill module only if at least one of the
   following is observed and attached as evidence:
   - a failing assertion in a skill/dream suite
     (`tests/test_skill_auto_repair.py`, `tests/test_fleet_skills.py`,
     `tests/test_hermes_skills_dedup.py`, or an adjacent skill test) that
     reproduces on the current tree;
   - a concrete reproducer or stack trace localized to a specific skill module
     or checked-in skill asset; or
   - at least two independent, non-self-referential evidence records (support
     `>= 2`) pointing at the same skill component, which would also raise the
     consolidation confidence above `low`.
   Absent such evidence, the finding remains not actionable as a skill defect.

## Assumptions

- This is a `repo_change` task whose upstream determination is NOT ACTIONABLE,
  so the tracked deliverable is this close-out note rather than a code repair.
  Recorded here so the remediation outcome is auditable from repository history.
- Canonical synchronization, final tests/CodeGraph, commits of tracked
  modifications, and publication are owned by the deterministic host finalizer;
  this note is self-contained and unaffected by upstream drift.
