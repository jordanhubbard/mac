# Investigation: dream `tool_or_skill_name` (skill) finding — actionability & root signal

**Parent task**: `task_ae123a32896b49b9b415b3395bee34a4` (deliverable absent; working
tree clean, so ground truth re-established here).
**Scope**: the `skill` / `tool_or_skill_name` dream finding surfaced by
`src/mac/dream_scanner.py`, classified low-confidence by
`src/mac/dream_cycle_classifier.py`, and filed as a follow-up by
`src/mac/dream_repair_tasks.py` (`file_low_confidence_repair_tasks`,
`_task_description`).
**Investigated by**: fleet worker (read-only analysis; no production code changed).

## Verdict: not-actionable / evidence-gap

The `tool_or_skill_name` finding is **not actionable as a skill defect**. Its
sole signal is a *name mention* ("skill referenced: `<name>`"), not a failure. No
root failure signal is attached because the scanner never attaches one to this
kind. The correct remediation is a small classifier/repair-filing change so this
inventory-only kind stops minting low-confidence repair tasks; that is
**actionable-plan-only** and is scoped below.

## Root signal behind the finding

`src/mac/dream_scanner.py` emits the `tool_or_skill_name` candidate kind purely
as a *name inventory*, always at `severity="info"`, from six call sites
(`src/mac/dream_scanner.py:240`, `:249`, `:258`, `:331`, `:430`, `:440`).

- For a skill, the emit uses signature `skill:<name>`, dimension
  `skill_name=<name>`, and an excerpt built as `"skill referenced: <name>"`
  (`src/mac/dream_scanner.py`, `_scan_hermes_message` and `_emit_names_from_text`).
  Skill names come from `_extract_skill_names()` scraping `skills/…`, `$name`, or
  `skill: name` tokens out of message/event text.
- The distinct root-cause kinds (`repeated_failure`, `tool_call_error`,
  `test_failure`, `model_provider_error`) are emitted *separately* and carry the
  redacted failure excerpt and `_failure_signature(text)`. The
  `tool_or_skill_name` candidate deliberately does **not** carry that failure
  text.

So the concrete "root signal" for this finding is: *a skill name (e.g. `codex`)
was seen in some session/ledger text.* There is no failure, traceback, exit
status, or provider error bound to it — by design.

## Why it lands as a low-confidence repair task (the evidence gap)

Traced end-to-end against the bootstrapped `.venv`:

1. **Classifier sees no signal.** `classify_candidate()` builds its match text
   only from `summary`, `kind`, `scope`, `observations`, `record_type_counts`
   keys, and `retrieval.query_terms` (`_combined_text`,
   `src/mac/dream_cycle_classifier.py`). A scanner candidate carries the skill
   name in `signature` (`skill:codex`) and `dimensions.skills`, **neither of
   which `_combined_text` reads**. Result: `areas == []` and
   `overall_confidence == "low"` (score 0.35). Reproduced directly:

   ```
   classify_candidate({kind:"tool_or_skill_name", signature:"skill:codex",
     dimensions:{skills:[{name:"codex",count:1}]}, evidence:[{excerpt:"skill referenced: codex"}]})
   # -> overall_confidence="low" (0.35), areas=[]
   ```

2. **Repair filing still fires.** `file_low_confidence_repair_tasks()` gates on
   `overall_confidence == "low"` **and** `any(affected.values())`
   (`src/mac/dream_repair_tasks.py`). `_affected_labels()` recovers the skill a
   *second* way the classifier ignores: from the `signature` `skill:` prefix and
   from `dimensions.skills`. So `affected["skills"] == ["codex"]` is non-empty and
   a follow-up task is minted — even though the classifier found zero areas.
   Reproduced: `_affected_labels(...)` -> `{"skills": ["codex"], ...}`,
   `any(...) == True`.

3. **The filed task has nothing to act on.** `_task_description()` prints the
   affected labels, the candidate summary, and the candidate evidence. For this
   kind the only evidence excerpt is `"skill referenced: codex"`, so the
   generated acceptance criterion ("Confirm whether the finding is actionable
   from the attached evidence") resolves to *not actionable*: there is no failure
   to reproduce or repair.

This is a **classifier ↔ repair-filing inconsistency**: the classifier's
signal-detection intentionally ignores `signature`/`dimensions`, but the
repair-filing affected-label extraction reads exactly those fields, so an
inventory-only "name was mentioned" candidate slips through the `low + affected`
gate.

## Exact files / behavior a fix would touch

The remediation is a filing-gate refinement, not a skill-behavior change:

- `src/mac/dream_repair_tasks.py` — `file_low_confidence_repair_tasks()`: skip
  candidates whose `kind == "tool_or_skill_name"` (an info-severity name
  inventory), or, more generally, require at least one evidence record with a
  failure-style excerpt / a non-`info` severity before filing. This is the single
  smallest, most-targeted change and keeps the dedupe/fingerprint logic intact.
- Optional companion in `src/mac/dream_cycle_classifier.py` — `_combined_text()`
  could also fold in `signature` and `dimensions.skills/tools` so classifier area
  detection and repair-filing agree; but this would *raise* detected areas rather
  than suppress filing, so it must be paired with the gate above to avoid filing
  more inventory tasks.

`src/mac/skill_auto_repair.py` is **not** involved: it only engages at
`overall_confidence == "high"` and guards writes under `skills/` /
`deploy/skills/`. A `tool_or_skill_name` finding is `low` by construction and
never reaches it.

## Ground truth observed

Measured in the task-owned worktree with the bootstrapped `.venv`
(`python3 scripts/bootstrap-project.py`). All sources read, none modified.

`.venv/bin/pytest tests/test_dream_scanner.py tests/test_dream_cycle_classifier.py
tests/test_dream_repair_tasks.py tests/test_skill_auto_repair.py
tests/test_fleet_skills.py -q` → **127 passed**.

The suites document the exact behavior above:
- `tests/test_dream_scanner.py::test_scans_model_provider_errors_and_skill_tool_names`
  asserts a `skill:codex` `tool_or_skill_name` candidate with
  `dimensions.skills == [{"name": "codex", "count": 1}]` and no failure text.
- `tests/test_dream_cycle_classifier.py` low-signal cases confirm a candidate with
  no distinguishing summary text stays `overall_confidence == "low"` (0.35).
- `tests/test_dream_repair_tasks.py::test_files_low_confidence_task_with_evidence_and_labels`
  confirms `affected.skills` is recovered and a task is filed for a low-confidence
  candidate.

## Determination

- **Actionability**: not-actionable / evidence-gap *as a skill defect* — the
  finding is a name mention with no root failure signal.
- **Recommended remediation** (actionable-plan-only): tighten
  `file_low_confidence_repair_tasks()` in `src/mac/dream_repair_tasks.py` to not
  file repair tasks for the info-only `tool_or_skill_name` kind (or require a
  failure-style evidence excerpt / non-`info` severity), closing the
  classifier↔filing inconsistency. No skill-module or `skill_auto_repair.py`
  change is warranted.

## Reproduction

```
python3 scripts/bootstrap-project.py
.venv/bin/pytest tests/test_dream_scanner.py tests/test_dream_cycle_classifier.py \
  tests/test_dream_repair_tasks.py tests/test_skill_auto_repair.py \
  tests/test_fleet_skills.py -q
# => 127 passed
```
