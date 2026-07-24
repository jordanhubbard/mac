!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Investigation: dream finding with a generic `skill` affected label — placeholder area bucket, not a defect location

**Parent task**: `task_89a497ca1b1a46ef96bf13258660b9e0`
(repair-prerequisite `task_repair_8c81157ac9eb538d6fddf16a`). Working tree clean
at investigation time, so ground truth is re-established here.
**Scope**: the low-confidence dream-cycle finding whose only affected label is a
bare `skill` (no concrete skill/tool/provider/repo_area), traced end-to-end
through `src/mac/dream_scanner.py`, `src/mac/nap_consolidator.py`,
`src/mac/dream_cycle_classifier.py`, and `src/mac/dream_repair_tasks.py`.
**Investigated by**: fleet worker (read-only analysis; no production code, skill,
or tool changed).

## Verdict: NOT a concrete defect location — generic placeholder area bucket

The generic `skill` affected label is a **placeholder area bucket**, not a
concrete defect location. It names the literal word *"skill"*, produced by a
bare `\bskill[s]?\b` word-match in the classifier's area table — there is no
concrete skill, tool, provider, or repo_area target behind it. The finding's
`low` confidence is a **heuristic artifact of a single self-referential evidence
record** (support < 2), not evidence of a reproducible defect. All six required
suites pass on the current tree; nothing in the skill/tool/dream subsystem
misbehaves.

## Exact modules / functions that produce kind, scope, confidence, and label

Traced and live-reproduced against the bootstrapped `.venv`.

### 1. Kind and confidence — `src/mac/nap_consolidator.py`

- `_dream_kind(records)` returns `failure_pattern` whenever the joined record
  text contains `failure` / `failed` / `error`. The sole evidence record is a
  prior *repair failed* memory, so its text trivially matches and the artifact is
  bucketed `failure_pattern` (scope `project`, because a project is present).
- `_confidence_for_records(records)` returns `("low", 0.35)` for `support < 2`.
  With exactly one supporting record the artifact is **necessarily** low
  confidence. Reproduced: `_confidence_for_records([one_record]) == ('low', 0.35)`.

### 2. Generic `skill` area — `src/mac/dream_cycle_classifier.py`

- `classify_candidate()` builds match text via `_combined_text()` from
  `summary`, `kind`, `scope`, `observations`, `record_type_counts` keys, and
  `retrieval.query_terms` — it does **not** read `signature` or
  `dimensions.skills`.
- The area table `_SKILL_PATTERNS` opens with `(r"\bskill[s]?\b", "skill")`. The
  consolidated summary contains the plain word "skill" (e.g. "...low-confidence
  **skill** finding..."), so this bare pattern fires and emits an area with
  `area_type="skill"`, `area_name="skill"` — the canonical bucket name, **not a
  discovered skill name**.
- `CONFIDENCE_THRESHOLDS` maps `low -> 0.35`. With one evidence record and one
  signal, `_confidence_for()` returns `low`, so `overall_confidence == "low"`
  (0.35). Reproduced: `classify_candidate(...)["areas"] ==
  [{area_type:"skill", area_name:"skill", confidence:"low", ...}]`,
  `overall_confidence == "low"`.

### 3. Affected label and gate — `src/mac/dream_repair_tasks.py`

- `_affected_labels()` copies the classifier's `skill` area into
  `affected["skills"] == ["skill"]`. (It also recovers concrete names from a
  `signature` `skill:` prefix or `dimensions.skills`, but this candidate carries
  neither, so the only label is the literal word "skill".)
- `file_low_confidence_repair_tasks()` gates on
  `overall_confidence == "low"` **and** `any(affected.values())`. Because
  `affected["skills"] == ["skill"]` is non-empty, the gate passes and a follow-up
  task is minted even though nothing concrete is named.
- `_is_inventory_only_candidate()` does **not** suppress this candidate:
  `DREAM_INVENTORY_ONLY_KINDS == {"tool_or_skill_name"}`, but this candidate's
  kind is `failure_pattern`, so the inventory-only skip does not apply.
- `repair_fingerprint()` hashes normalized `{kind, scope, project, signature,
  summary, affected}`; the `affected` map with the generic `skill` label is part
  of the dedupe key. The fingerprint is a **dedupe key for a dream candidate**,
  not a pointer into any skill module.
- `_task_description()` prints the affected labels, summary, and evidence, then a
  generic acceptance criterion ("Confirm whether the finding is actionable from
  the attached evidence") — which, with only a name-mention signal, resolves to
  *not actionable*.

## Root signal

A **single self-referential memory record** — a prior "repair failed for a
low-confidence skill finding" learning — is the entire evidentiary basis. That
one record:

- makes `_dream_kind` pick `failure_pattern` (its text says "failed"),
- makes `_confidence_for_records` return `low` (support = 1, < 2),
- puts the plain word "skill" into the summary, which the classifier's bare
  `\bskill[s]?\b` pattern turns into a generic `skill` area bucket, which
  `_affected_labels` surfaces as `affected["skills"] == ["skill"]`.

So both the low confidence and the generic `skill` label are **heuristic
artifacts of one self-referential record**, not signals of a reproducible skill,
tool, or repo defect. `src/mac/skill_auto_repair.py` is **not** involved: it only
engages at `overall_confidence == "high"` and never sees a low-confidence
finding.

## Verification

Bootstrapped the task-owned worktree with `python3 scripts/bootstrap-project.py`
(`python3` 3.12.13; `git`/`gh` present; pytest/coverage installed into `.venv`).
All sources were read, none modified.

```
.venv/bin/pytest tests/test_dream_repair_tasks.py tests/test_dream_cycle_classifier.py \
  tests/test_nap_consolidator.py tests/test_skill_auto_repair.py \
  tests/test_fleet_skills.py tests/test_hermes_skills_dedup.py -q
# => 142 passed, 0 failed
```

Live reproduction (read-only) confirmed the exact chain:

- `nap_consolidator._dream_kind([repair-failed record]) == "failure_pattern"`.
- `nap_consolidator._confidence_for_records([one record]) == ("low", 0.35)`.
- `dream_cycle_classifier.classify_candidate(...)` → `overall_confidence == "low"`
  (0.35); `areas == [{"area_type": "skill", "area_name": "skill",
  "signals": ["\\bskill[s]?\\b"], ...}]`.
- `dream_repair_tasks._affected_labels(...)` → `{"skills": ["skill"], "tools":
  [], "providers": [], "repo_areas": []}`; `any(affected.values()) == True`.
- `dream_repair_tasks._is_inventory_only_candidate(...) == False`
  (kind `failure_pattern` is not in `DREAM_INVENTORY_ONLY_KINDS`).
- `repair_fingerprint(...)` produced a stable `dreamrepair:` key from the
  normalized candidate + generic `skill` affected map.

## Determination

- **Is the `skill` label a concrete defect location?** No. It is a generic
  placeholder area bucket (the literal word "skill" matched by
  `\bskill[s]?\b`), carrying no concrete skill/tool/provider/repo_area.
- **Is the low confidence a heuristic artifact of a single self-referential
  record (support < 2)?** Yes. One record drives kind, confidence, and label.
- **Reproducible defect?** None. 142/142 required tests pass; the subsystem
  behaves as specified.
- **Recommended remediation** (plan-only; out of scope for this read-only note):
  a filing-gate refinement in `file_low_confidence_repair_tasks()` /
  `_affected_labels()` so a generic single-token bucket (e.g. `affected` whose
  only label equals the area-type word such as `"skill"`) does not satisfy the
  `low + affected` gate — mirroring the existing `DREAM_INVENTORY_ONLY_KINDS`
  guard. No skill-module or `skill_auto_repair.py` change is warranted.

## Reproduction

```
python3 scripts/bootstrap-project.py
.venv/bin/pytest tests/test_dream_repair_tasks.py tests/test_dream_cycle_classifier.py \
  tests/test_nap_consolidator.py tests/test_skill_auto_repair.py \
  tests/test_fleet_skills.py tests/test_hermes_skills_dedup.py -q
# => 142 passed
```
