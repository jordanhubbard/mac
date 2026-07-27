# Probe: Low-Confidence "skill" Dream Finding

Independent probe (plan node `probe`) for parent audit
`task_73e4c903b5eb45728e438b5256fba5c7` (title: "Investigate low-confidence
dream finding: skill"). This is a read-only investigation of the repository's
skill surface. It records ground truth only and changes **no** `src/`, `tests/`,
`skills/`, or `deploy/` file. The purpose is to independently establish whether
the dream-repair finding fingerprint
`dreamrepair:25a0fdad55bbcbb229620b6f2ee99af6` names a concrete, fixable defect
or is a low-signal evidence gap.

## Verdict

**NOT ACTIONABLE — evidence gap.** The finding is a low-confidence, generic,
self-referential `failure_pattern` with no named skill, tool, provider, or repo
area. The repository's live skill surface is healthy and the worktree is clean.
There is no concrete, reproducible defect for a code or skill change to fix.

## Finding Claim (from parent `metadata.dream_repair`)

- fingerprint: `dreamrepair:25a0fdad55bbcbb229620b6f2ee99af6`
- kind: `failure_pattern`; scope: project `mac`
- confidence: `low` (overall_confidence_score = 0.35)
- affected skills: `['skill']` — a bare generic label, not a skill name
- affected tools / providers / repo_areas: none
- evidence_count: `1`
- signals: `['\bskill[s]?\b']` — a plain word-boundary token match

Every discriminating field is empty or a generic placeholder. The lone signal
is the English token "skill", which matches any log line mentioning a skill in
passing. A `failure_pattern` with support = 1 and a bare-token signal is the
shape source heuristics score lowest; 0.35 reflects support < 2, not a diagnosed
fault.

## Independent Skill-Surface Inspection

The repository's entire skill surface is two files, both healthy:

- `skills/mac-agent-terminal-timeout/SKILL.md` — 137 lines, valid YAML
  frontmatter (`name`, `description`, `version 1.1.0`, `platforms`,
  `metadata.hermes.tags`, `related_skills`). Coherent "when to use / root cause /
  fix" body about the `terminal:timeout` tool_error. No broken directives or
  dangling references.
- `skills/setup-mac-fleet/SKILL.md` — 224 lines, valid frontmatter (`name`,
  `description`). Coherent setup/deploy workflow. No broken directives.

`git status --porcelain` on the task worktree is clean (no dirty tracked files).
No specific skill, tool, provider, or repo area is named anywhere in the finding
beyond the generic token "skill".

## Actionability Decision

Against the criterion — *is there a concrete, reproducible defect in a skill or
tool that a code/skill change would fix?* — the answer is **no**:

- No named target: `affected_skills=['skill']` is a placeholder; tools,
  providers, and repo_areas are empty.
- No reproducible failure: the signal is a bare-token text match, not a stack
  trace, error signature, or failing test.
- Low confidence by construction: 0.35 stems from support < 2, not a diagnosed
  fault; both live SKILL.md files are healthy and the tree is clean.

There is nothing to fix. Fabricating a "repair" would edit a healthy skill on
the basis of a non-defect and is out of scope for this read-only probe. This
independent probe corroborates the sibling audit recorded in `findings.md`.

## Deliverable to the Disposition Child

- **Decision:** NOT ACTIONABLE (close finding
  `dreamrepair:25a0fdad55bbcbb229620b6f2ee99af6` as such).
- **Reopen criteria:** reopen only if a *named* skill or tool acquires a
  reproducible failure signature (a real error/stack/failing test) with at least
  two independent, non-self-referential evidence records.
- **No source repo change** is warranted: both `SKILL.md` files are healthy and
  the worktree is clean. This probe note is the only artifact.

## Verification Performed

- Read the finding's claim fields from parent task metadata (section above).
- Inspected both `SKILL.md` files (frontmatter + body) for defects: none found.
- Confirmed `git status --porcelain` on the worktree is clean and that no skill
  or tool is named beyond the generic token "skill".
- Ran the declared contract test command `scripts/run-contract-tests.sh` to
  confirm the tree remains healthy under this probe.
- No `src/`, `tests/`, `skills/`, or `deploy/` file was edited by this probe.
