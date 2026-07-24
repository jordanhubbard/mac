!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Investigation: dream finding `dreamrepair:173ce952` with a generic `tests` affected label — placeholder area bucket, not a defect location

**Parent task**: `task_5c76e105cf264a3ab40d1f3fe67006f5`
(repair-prerequisite `task_repair_ca6573ddc8faa4a0fe43de35`). Working tree clean
at investigation time, so ground truth is re-established here.
**Scope**: the low-confidence dream-cycle finding `dreamrepair:173ce952` whose
only affected label is a bare `tests` repo-area bucket (no concrete failing
test, module, or scenario), traced end-to-end through
`src/mac/nap_consolidator.py`, `src/mac/dream_cycle_classifier.py`, and
`src/mac/dream_repair_tasks.py`.
**Investigated by**: fleet worker (read-only analysis; no production code, test,
skill, or tool changed).

## Verdict: NOT a concrete defect location — generic placeholder area bucket

The generic `tests` affected label is a **placeholder area bucket**, not a
concrete defect location. It names the mac `tests/` tree area, produced by a
bare `\btests/\w+` path-word match in the classifier's `repo_area` table — there
is no concrete failing test, test module, or reproducible test defect behind it.
The finding's `low` confidence is a **heuristic artifact of a single
self-referential evidence record** (support < 2), not evidence of a reproducible
defect. All required suites pass on the current tree; nothing in the `tests/`
tree or the dream subsystem misbehaves.

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

### 2. Generic `tests` area — `src/mac/dream_cycle_classifier.py`

- `classify_candidate()` builds match text via `_combined_text()` from
  `summary`, `kind`, `scope`, `observations`, `record_type_counts` keys, and
  `retrieval.query_terms` — it does **not** read `signature` or a concrete test
  target.
- The repo-area table `_REPO_AREA_PATTERNS` contains
  `(r"\btests/\w+", "tests")`. The consolidated summary mentions a `tests/...`
  path fragment (e.g. "...scoped to `tests/test_dream_repair_tasks.py` in the mac
  tests area..."), so this bare pattern fires and emits an area with
  `area_type="repo_area"`, `area_name="tests"` — the canonical bucket name for
  the whole `tests/` tree, **not a discovered failing test**.
- `CONFIDENCE_THRESHOLDS` maps `low -> 0.35`. With one evidence record and one
  signal, `_confidence_for()` returns `low`, so `overall_confidence == "low"`
  (0.35). Reproduced: `classify_candidate(...)["areas"] ==
  [{area_type:"repo_area", area_name:"tests", confidence:"low", ...}]`,
  `overall_confidence == "low"`.

### 3. Affected label and gate — `src/mac/dream_repair_tasks.py`

- `_affected_labels()` copies the classifier's `tests` repo-area into
  `affected["repo_areas"] == ["tests"]`. (It also recovers concrete names from a
  `signature` `repo_area:` prefix, but this candidate carries none, so the only
  label is the generic bucket word "tests".)
- `file_low_confidence_repair_tasks()` gates on
  `overall_confidence == "low"` **and** `any(affected.values())`. Because
  `affected["repo_areas"] == ["tests"]` is non-empty, the gate passes and a
  follow-up task is minted even though nothing concrete is named.
- `_is_inventory_only_candidate()` does **not** suppress this candidate:
  `DREAM_INVENTORY_ONLY_KINDS == {"tool_or_skill_name"}`, but this candidate's
  kind is `failure_pattern`, so the inventory-only skip does not apply.
- `repair_fingerprint()` hashes normalized `{kind, scope, project, signature,
  summary, affected}`; the `affected` map with the generic `tests` label is part
  of the dedupe key. The fingerprint is a **dedupe key for a dream candidate**,
  not a pointer into any test module.
- `_task_description()` prints the affected labels, summary, and evidence, then a
  generic acceptance criterion ("Confirm whether the finding is actionable from
  the attached evidence") — which, with only a name-mention signal, resolves to
  *not actionable*.

## Root signal

A **single self-referential memory record** — a prior "repair failed for a
low-confidence finding scoped to the mac tests area" learning — is the entire
evidentiary basis. That one record:

- makes `_dream_kind` pick `failure_pattern` (its text says "failed"),
- makes `_confidence_for_records` return `low` (support = 1, < 2),
- puts a `tests/...` path fragment into the summary, which the classifier's bare
  `\btests/\w+` pattern turns into a generic `tests` repo-area bucket, which
  `_affected_labels` surfaces as `affected["repo_areas"] == ["tests"]`.

So both the low confidence and the generic `tests` label are **heuristic
artifacts of one self-referential record**, not signals of a reproducible test,
module, or repo defect. No test in `tests/` is failing or flaky as a result of
this finding.

## Verification

Bootstrapped the task-owned worktree with `python3 scripts/bootstrap-project.py`
(`python3` 3.12; `git`/`gh` present; pytest/coverage installed into `.venv`).
All sources were read, none modified.

```
.venv/bin/pytest tests/test_dream_repair_tasks.py tests/test_dream_cycle_classifier.py \
  tests/test_nap_consolidator.py -q
# => 90 passed, 0 failed
```

Live reproduction (read-only) confirmed the exact chain:

- `nap_consolidator._dream_kind([repair-failed record]) == "failure_pattern"`.
- `nap_consolidator._confidence_for_records([one record]) == ("low", 0.35)`.
- `dream_cycle_classifier.classify_candidate(...)` → `overall_confidence == "low"`
  (0.35); `areas == [{"area_type": "repo_area", "area_name": "tests",
  "signals": ["\\btests/\\w+"], ...}]`.
- `dream_repair_tasks._affected_labels(...)` → `{"skills": [], "tools": [],
  "providers": [], "repo_areas": ["tests"]}`; `any(affected.values()) == True`.
- `dream_repair_tasks._is_inventory_only_candidate(...) == False`
  (kind `failure_pattern` is not in `DREAM_INVENTORY_ONLY_KINDS`).
- `repair_fingerprint(...)` produced a stable `dreamrepair:` key from the
  normalized candidate + generic `tests` affected map.

## Determination

- **Is the `tests` label a concrete defect location?** No. It is a generic
  placeholder area bucket (the mac `tests/` tree matched by `\btests/\w+`),
  carrying no concrete failing test, module, or scenario.
- **Is the low confidence a heuristic artifact of a single self-referential
  record (support < 2)?** Yes. One record drives kind, confidence, and label.
- **Reproducible defect?** None. The required subsystem suites pass; the
  `tests/` tree and dream subsystem behave as specified.
- **Recommended remediation** (plan-only; out of scope for this read-only note):
  a filing-gate refinement in `file_low_confidence_repair_tasks()` /
  `_affected_labels()` so a generic single-token repo-area bucket (e.g.
  `affected` whose only label is a bare tree name such as `"tests"` with no
  concrete path segment) does not satisfy the `low + affected` gate — mirroring
  the existing `DREAM_INVENTORY_ONLY_KINDS` guard. No test-module change is
  warranted.

## Reproduction

```
python3 scripts/bootstrap-project.py
.venv/bin/pytest tests/test_dream_repair_tasks.py tests/test_dream_cycle_classifier.py \
  tests/test_nap_consolidator.py -q
# => 90 passed
```
