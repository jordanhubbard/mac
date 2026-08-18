!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Triage: Dream Finding `dreamrepair:828e1ef4a530935a9a7db4b1807202e1`

Read-only corroboration for triage task
`task_bd52602a4ea446f1a9ca1936f9d160c2` (title: "Triage dream finding
dreamrepair:828e1ef4: corroborate the single evidence record"), a cooperative
child of the parent audit `task_bcbebedf59404975a16fb17f6f9f6032`
("Investigate low-confidence dream finding: skill"). This document records
ground truth only. No `src/`, `tests/`, `skills/`, or `deploy/` file was
changed. All facts below were read from the live MAC hub read API
(`GET /health`, `GET /memory`, `GET /tasks/{id}`).

## Summary Verdict

**NOT ACTIONABLE — evidence gap.** The finding's sole supporting record,
`mem_9c807de962da4ecda4eac62670006672`, is a **mislabeled `[success]`
`plan_decomposed`** record, not a failure. The `'skill'` label is a **generic
regex placeholder** (`\bskill[s]?\b`), not a concrete named skill or defect. The
evidence is **self-referential across a chain of prior identical investigation
tasks**: each "Investigate low-confidence dream finding: skill" task, when
decomposed, emits a success record that the nap/dream cycle re-classifies as a
new low-confidence `failure_pattern`, spawning the next identical task. There is
no reproducible defect for a code or skill change to fix.

## Corroboration Method (hub read API)

- `GET $MAC_HUB_URL/health` → `{"status":"ok"}`.
- `GET /memory?task_id=task_5568db6185174de5ab295031da3246d2&limit=100` returned
  exactly 3 records for the candidate task; the target record is present.
- `GET /tasks/task_5568db6185174de5ab295031da3246d2` and
  `GET /tasks/task_70007fd6f9a64bc8bb434873f3c3dc9c` supplied the
  `metadata.dream_repair` classification blocks used below.

## The Single Supporting Evidence Record

`mem_9c807de962da4ecda4eac62670006672`
- `task_id`: `task_5568db6185174de5ab295031da3246d2`
- `record_type`: `deployment_learning:mac`; `created_by`: `mac-task-executor`
- content (`mac.deployment_learning.v1`):
  `evidence_type = "plan_decomposed"`, `outcome = "success"`,
  `error_signature = ""`, `signals = {returncode: 0, tests: null, ...}`,
  `task_title = "Investigate low-confidence dream finding: skill"`.

The derived dream artifact `mem_ba0c35995e20434f9097d313a5d77b97`
(`dream:failure_pattern`, `mac.dream.v1`, `confidence_score = 0.35`,
`evidence_count = 1`) records the observation verbatim as:
`"[success] Investigate low-confidence dream finding: skill (plan_decomposed)"`.

## Answers to the Three Triage Questions

1. **Genuine failure or a mislabeled `[success]` `plan_decomposed` record?**
   Mislabeled. The record's own `outcome` is `success` and its
   `evidence_type` is `plan_decomposed` (a task-decomposition bookkeeping event,
   `returncode = 0`, empty `error_signature`). The `failure_pattern` kind comes
   from the dream classifier grouping, not from any observed failure.

2. **Is `'skill'` a concrete named defect or a generic regex placeholder?**
   Generic placeholder. In `src/mac/dream_cycle_classifier.py` the first
   `_SKILL_PATTERNS` entry `(r"\bskill[s]?\b", "skill")` maps any occurrence of
   the bare word "skill"/"skills" to the canonical `area_name = "skill"`. The
   candidate summary literally contains the token ("...dream finding: skill..."),
   so the label is a self-fulfilling word-boundary match, not a named skill.
   `affected_tools/providers/repo_areas` are all empty; the two live SKILL.md
   files (`skills/mac-agent-terminal-timeout`, `skills/setup-mac-fleet`) are
   healthy.

3. **Is the evidence self-referential across a chain of prior identical
   investigation tasks?** Yes. Traced links (each finding's sole evidence is the
   prior identical task's `plan_decomposed` success):
   - `dreamrepair:828e1ef4a530935a9a7db4b1807202e1` (this finding)
     ← candidate `task_5568db6185174de5ab295031da3246d2`
     ← evidence `mem_9c807de962da4ecda4eac62670006672` (plan_decomposed/success).
   - `task_5568db...`'s own `metadata.dream_repair` =
     `dreamrepair:6b7c8892c8628c6d28a51438528b904d`, candidate
     `task_70007fd6f9a64bc8bb434873f3c3dc9c`, evidence
     `mem_5c221c62251c4d0688d153276b766c7b` (plan_decomposed/success).
   - `task_70007fd6...`'s own `metadata.dream_repair` =
     `dreamrepair:cf8fa7ce5d85c7e0922d419443055a43`, candidate
     `task_8764c80cdff34d20867478306b7387a0`, evidence
     `mem_22ba025a49a0453483d1ca0882181893`.
   All share the identical title "Investigate low-confidence dream finding:
   skill" and the identical summary shape
   "...Supported by 1 memory record(s): [success] ... (plan_decomposed)". This
   is a feedback loop: the pattern re-derives itself from its own prior
   decomposition.

## Recorded Evidence Gap

Support is effectively **zero net-new**: a single low-confidence
(`0.35`, `evidence_count = 1`), self-referential record that is a
`plan_decomposed` **success**, labeled by a generic `\bskill[s]?\b` regex with no
named skill/tool/provider/repo-area and no reproducible failure signature
(empty `error_signature`, `returncode = 0`). Confidence `0.35` reflects
support < 2, not a diagnosed fault.

## Disposition Handoff

- **Decision:** NOT ACTIONABLE. Recommend closing
  `dreamrepair:828e1ef4a530935a9a7db4b1807202e1` and treating the identical-title
  chain as a classifier feedback loop rather than fresh failure evidence.
- **Reopen criteria:** a *named* skill or tool acquiring a reproducible failure
  signature (real error/stack/failing test) with at least two independent,
  non-self-referential evidence records.
- **No skill/tool changes** were made, per task scope.
