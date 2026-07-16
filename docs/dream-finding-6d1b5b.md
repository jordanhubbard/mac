# Dream-Finding Assessment: dreamrepair:6d1b5bbe0a13515fef0bd061ef001119

**Task**: Assess actionability of a low-confidence dream-cycle repair finding
scoped to the *skill* subsystem for project `mac`.
**Parent task**: task_886a1a7002b94d7aa796a445a5f25a00
(goal: "Investigate low-confidence dream finding: skill").
**Finding fingerprint**: `dreamrepair:6d1b5bbe0a13515fef0bd061ef001119`
**Origin task of the one evidence record**: task_fd2f34b64823410c84a14fc0345610ff
**Evidence record**: mem_6af8d85aa3484f0ab4ab6212310ba2b5
(record_type `deployment_learning:mac`)
**Assessed by**: fleet worker (dream-finding review; no skill/tool edits)
**Assessment date**: 2026-07-16

## Status: NOT ACTIONABLE — generic label noise, not a concrete skill defect

The finding is a **failure_pattern** with **project** scope and **low**
confidence (score 0.35), backed by exactly **one** evidence record. The
classifier's only match against the `skill` area is the bare-token regex
`\bskill[s]?\b`; no specific skill asset, tool, provider, or repo-area label is
attached. Investigation confirms this is a self-referential, second-order
artifact of a *prior* investigation, not evidence of a defect in the checked-in
skill subsystem. No skill or tool should be changed on the strength of this
finding.

## What the Evidence Does and Does Not Establish

**Does establish:**

- A dream-cycle candidate exists whose text contains the token `skill`, which
  is sufficient for the classifier to tag the `skill` area at its floor
  confidence (single-record → `low` → 0.35). This is reproducible against the
  checked-out classifier (see "Reproduction" below).
- The candidate summary — a `failure_pattern` reading
  "Repair environment prerequisites: Investigate low-confidence dream finding:
  skill (repo_change)" — is itself the recap of a prior *environment-prerequisite
  repair* task (origin task_fd2f34b64823410c84a14fc0345610ff). That prior task
  already closed its finding (`dreamrepair:5404b15fffa355d739c21e138c5cc122`) as
  NOT-ACTIONABLE; see `docs/prereq-task-fd2f34.md`. This finding is therefore a
  derivative of that earlier recap, not an independent observation.

**Does not establish:**

- No concrete skill defect. The single evidence record is a meta-observation
  about a prior investigation ("investigated the skill subsystem and ran the
  canonical contract"), not an observation of a failing skill: there is no
  failing assertion, no stack trace, no reproduction, and no named offending
  skill asset.
- No tool/provider/repo-area signal. The classifier attached only the bare
  `skill` token and no more specific label, which is the classic shape of an
  over-broad classification rather than a targeted defect.
- No causal link to any checked-in skill file. The two tracked skill assets and
  the fleet deploy skill import and pass their guards and suites unchanged.

## Reproduction

Running the checked-out classifier over a candidate matching this finding's
shape (single evidence record; only the bare `skill` token present) reproduces
the finding exactly:

- `area_type = skill`, `area_name = skill`
- `confidence = low`, `confidence_score = 0.35`
- `signals = [\bskill[s]?\b]` (the single bare-token pattern)
- `overall_confidence = low` (0.35)

This matches the classifier's documented low-confidence rule in
`src/mac/dream_cycle_classifier.py`: "Signal is present in the artifact text,
but only a single evidence record backs it. Score ~= 0.35." The score is a
floor produced by one weak keyword match, not a measure of a real defect.

## Skill Subsystem Health (checked-out source)

Confirmed in the task-owned worktree against the checked-in code:

- Imports clean: `mac`, `mac.skill_auto_repair`, `mac.dream_repair_tasks`,
  `mac.dream_scanner`, `mac.dream_cycle_classifier`.
- Focused skill/dream suites pass: `test_skill_auto_repair.py`,
  `test_dream_repair_tasks.py`, `test_dream_scanner.py`,
  `test_dream_cycle_classifier.py`, `test_fleet_skills.py`,
  `test_hermes_skills_dedup.py`, `test_dream_cycle_runner.py`
  (**156 passed**).
- The guarded skill auto-repair path enforces its documented guards (path
  allowlist under `skills/` and `deploy/skills/`, evidence-excerpt gate, secret
  scrub, operator-identity scrub) and its suite passes.
- Checked-in skill assets are present and covered by the fleet-generic docs
  guard (`test_docs_no_operator_identity`):
  `skills/mac-agent-terminal-timeout/SKILL.md`,
  `skills/setup-mac-fleet/SKILL.md`, and
  `deploy/skills/fleet/nvidia-inference-multimodal/SKILL.md`.

## Is the Finding Actionable?

**No.** It is a single-record, low-confidence (0.35) classification whose only
signal is a bare `skill` keyword match on the recap text of a prior
investigation task. It points to no concrete skill/tool/provider/repo-area
defect, and the skill subsystem is healthy under the checked-out source. Making
a skill or tool change now would be acting on label noise.

## Specific Behavior to Verify Next (before any repair)

If the fleet still wants a positive signal before closing the parent, verify
the following — none of which requires editing a skill or tool:

1. **Runtime preflight, not skill code.** The prior chain's real, repeated
   blocker was a coding-agent sandbox preflight failure at probe time
   (`class=probe_failed`) that fell back to the gateway. Confirm whether that
   preflight/probe path is the actual source of the recurring event; if so it
   belongs to the executor/runtime-availability path (probe/gateway), not the
   skill subsystem.
2. **Classifier label granularity.** Confirm that a bare `\bskill[s]?\b` match
   with a single evidence record should surface a `skill`-area finding at all,
   or whether such single-token, single-record candidates should be suppressed
   or require a more specific skill signal (e.g. `hermes.skill`,
   `skill_bundle`, a named `SKILL.md`) before filing. This is the lever most
   likely to stop the recurring low-confidence `skill` findings at the source.
3. **Evidence provenance guard.** Confirm whether a candidate whose sole
   evidence record is itself a prior investigation's recap (a
   `deployment_learning` meta-observation) should be excluded from repair-task
   filing, to prevent second-order findings that re-derive an already-closed
   NOT-ACTIONABLE result.

## Recommendation

- Close the parent finding as **not actionable**: it is a single-record,
  low-confidence classification of a prior investigation's recap, not a skill
  source defect.
- If the underlying preflight fallback recurs, file it against the
  executor/runtime-availability path (probe/gateway) with a tool/provider label
  rather than the bare `skill` token, so future dream findings are targeted.
- Do **not** change any skill or tool on the basis of this finding.

## Assumptions

- This is a `repo_change` review task with a healthy environment, so the
  tracked deliverable is this investigation record rather than a code repair.
  Recorded here so the outcome is auditable from repository history.
- The finding's `dream_repair` metadata and the raw evidence record were not
  present in the task-local `task.json` copy available to the worker; this
  assessment relies on the task description's stated finding parameters
  (failure_pattern, project scope, confidence 0.35, regex `\bskill[s]?\b`,
  one evidence record) and reproduces them against the checked-out classifier.
- Canonical synchronization and any rebase are owned by the deterministic host
  finalizer; this record is self-contained and unaffected by upstream drift.
