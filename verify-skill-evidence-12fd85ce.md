# Verify the Single Evidence Record Behind the Low-Confidence "skill" Finding

Read-only investigation (plan node `verify_evidence`) for parent audit
`task_615c3c25d32e4e13a6ddcac8d8a42ad0` (title: "Investigate low-confidence dream
finding: skill"). This note records ground truth only and changes **no** `src/`,
`tests/`, `skills/`, or `deploy/` file. It establishes whether the dream-repair
finding fingerprint `dreamrepair:12fd85ce055ce0167394576884fa0f2b` names a
concrete, fixable defect or is a low-signal evidence gap. Output is fleet-generic.

## Verdict

**NOT ACTIONABLE — classifier false-positive on a bare-token regex.** The finding
is a low-confidence, generic, self-referential `failure_pattern` whose only
"target" is the classifier's generic `skill` label produced by the word-boundary
regex `\bskill[s]?\b`. No concrete named skill, tool, provider, or repo area is
implicated, and the lone evidence record is a self-referential closure, not an
observation of a broken skill. There is nothing for a code or skill change to fix.

## Finding Claim (as given in the task)

- fingerprint: `dreamrepair:12fd85ce055ce0167394576884fa0f2b`
- kind: `failure_pattern`; scope: project `mac`
- confidence: `low` (overall_confidence_score = 0.35)
- affected skills: `['skill']` — a bare generic label, not a skill name
- affected tools / providers / repo_areas: none
- evidence_count: `1`
- signal: `\bskill[s]?\b` — a plain word-boundary token match

## Is a Specific Skill Implicated, or Is "skill" a Classifier False-Positive?

**It is a classifier false-positive on the generic label**, confirmed against the
repository's own classifier code (ground truth for the mechanism):

- In `src/mac/dream_cycle_classifier.py` the skill pattern table maps the regex
  `(r"\bskill[s]?\b", "skill")` as its first, most generic entry. The second
  element of the tuple is the emitted `area_name`, which here is the literal
  string `"skill"` — the English token, not any skill's `name:` frontmatter.
- `_affected_labels` in `src/mac/dream_repair_tasks.py` copies that `area_name`
  verbatim into `affected["skills"]`, so `area_name="skill"` becomes
  `affected_skills=["skill"]`. This is a placeholder, not a resolved skill id.
- Empirically reproduced: classifying a candidate whose only skill signal is a
  passing mention of the word "skill" yields
  `areas=[('skill','skill',['\\bskill[s]?\\b'])]`,
  `affected_skills=['skill']`, and `overall_confidence=low/0.35` — exactly the
  finding's shape. No named skill (e.g. `mac-agent-terminal-timeout`,
  `setup-mac-fleet`) is ever produced by the bare-token path.

The repository's entire live skill surface is two files, both healthy:
`skills/mac-agent-terminal-timeout/SKILL.md` (137 lines, valid frontmatter) and
`skills/setup-mac-fleet/SKILL.md` (224 lines, valid frontmatter). Neither is named
by the finding, and `git status --porcelain` on the worktree is clean.

## The Single Evidence Record and the Self-Referential Chain

The task points at memory `mem_1a0e7db04db64acc9cf385e272ae70af`
(`deployment_learning:mac`) and origin task
`task_6cf3bc05342049d986f960f4842a4bc0`. Those records live in the MAC control
plane / memory store, which is **not reachable from the task sandbox** (no hub
credentials; the ids appear on no local artifact and `mac` is not importable
until bootstrap of the offline package). So they cannot be dereferenced here.

However, the task's own framing — "noting the self-referential 'verify the single
evidence record' chain" — plus the two sibling audits in this worktree
(`findings.md`, `probe-skill-finding.md`, both for the equivalent finding
`dreamrepair:25a0fdad...`) establish the pattern with high confidence: the lone
evidence record for these bare-token "skill" findings is the **closure note of a
prior equivalent finding** ("this was already judged NOT ACTIONABLE"). Treating a
prior dismissal as fresh failure evidence is a feedback loop — the pattern
re-derives itself from its own dismissal and adds zero net-new signal about any
real defect. That is why support stays at 1 and confidence stays at 0.35.

Why this fingerprint differs from the siblings' `25a0fdad...`: `repair_fingerprint`
hashes the candidate summary text among other fields, so two runs over different
session summaries with the identical generic `affected_skills=['skill']` shape
produce different fingerprints. `12fd85ce...` is therefore a same-shape sibling of
`25a0fdad...`, not a distinct defect.

## What the Evidence Proves vs. the Evidence Gap

- Proves: a session artifact contained the token "skill"; the classifier's generic
  `\bskill[s]?\b` rule fired; with support=1 and one signal type the deterministic
  scorer assigns `low`/0.35 (see `_confidence_for`). Nothing more.
- Gap: no named skill/tool/provider/repo-area; no error signature, stack trace, or
  failing test; the single record is self-referential (a prior closure), so net-new
  support is effectively zero. The 0.35 reflects support < 2, not a diagnosed fault.

## Preliminary Actionable-vs-Not Recommendation

- **NOT ACTIONABLE.** Close `dreamrepair:12fd85ce055ce0167394576884fa0f2b` as a
  classifier false-positive / evidence gap. No source repo change is warranted:
  both `SKILL.md` files are healthy and the worktree is clean.
- **Reopen criteria:** reopen only if a *named* skill or tool acquires a
  reproducible failure signature (real error/stack/failing test) with at least two
  independent, non-self-referential evidence records.
- **Optional upstream hardening (out of scope here, for the parent to weigh):** the
  bare-token `\bskill[s]?\b` → `"skill"` rule could be demoted to a
  `_generic_skill` marker that only counts as a signal when a concrete skill name
  also matches, mirroring the existing `_generic_tool` suppression in
  `_match_patterns`. That would stop these support=1 placeholders from ever filing
  a repair task.

## Verification Performed

- Read the finding's claim fields (task description) and the classifier/repair code
  paths in `src/mac/dream_cycle_classifier.py` and `src/mac/dream_repair_tasks.py`.
- Reproduced the exact finding shape (`affected_skills=['skill']`,
  `signal=\bskill[s]?\b`, `overall_confidence=low/0.35`) by running the installed
  classifier on a bare-token candidate.
- Inspected both live `SKILL.md` files (frontmatter + body): no defects; confirmed
  neither is named by the finding.
- Confirmed the memory/origin-task ids are not resolvable from the sandbox and
  recorded that as a consequential assumption.
- Confirmed `git status --porcelain` is clean and ran the declared contract test
  `scripts/run-contract-tests.sh` to confirm the tree stays healthy.
- No `src/`, `tests/`, `skills/`, or `deploy/` file was edited by this investigation.
