!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a
    current operating contract; use the numbered book and current runbooks for
    instructions.

# Disposition: low-confidence dream finding `skill` (`dreamrepair:828e1ef4a530935a9a7db4b1807202e1`) — not actionable

**Finding**: a low-confidence dream-repair `failure_pattern`, kind
`failure_pattern`, scope `project`, project `mac`, affected-skill label `skill`,
fingerprint `dreamrepair:828e1ef4a530935a9a7db4b1807202e1`, confidence `low`
(`overall_confidence_score = 0.35`, `evidence_count = 1`), backed by a single
evidence record.
**Evidence source**: this disposition consumes the corroboration triage
`docs/dream-triage-828e1ef4.md` (triage task
`task_bd52602a4ea446f1a9ca1936f9d160c2`, this task's declared dependency), which
characterized the sole supporting record `mem_9c807de962da4ecda4eac62670006672`
(`deployment_learning:mac`) from originating task
`task_5568db6185174de5ab295031da3246d2` ("Investigate low-confidence dream
finding: skill").
**Dispositioned by**: fleet worker (disposition only; no production code, test,
skill, config, or deploy edits).

## Disposition: CLOSE — NOT ACTIONABLE (mislabeled success + generic label + self-referential evidence)

Close the finding as **not actionable** as a defect of any MAC skill or tool
surface. The finding is the deterministic output of the dream/nap consolidation
heuristics acting on a single mislabeled `[success]` `plan_decomposed`
bookkeeping record, tagged by a generic `\bskill[s]?\b` regex placeholder, whose
provenance chains back through prior identical investigation tasks. No concrete,
reproducible defect exists for a skill, tool, config, or code change to fix, so
the correct handling is to record this disposition as investigation evidence and
make **no** change to skills, tools, config, or source.

## Acceptance-criteria evaluation

- **(1) Genuine, recurring failure or a mislabeled `[success]`
  `plan_decomposed` artifact?** A mislabeled artifact. The lone record
  `mem_9c807de962da4ecda4eac62670006672` has `outcome = "success"`,
  `evidence_type = "plan_decomposed"` (a task-decomposition bookkeeping event),
  `error_signature = ""`, and `returncode = 0`. The `failure_pattern` kind is
  assigned by the dream classifier's grouping, not by any observed failure. The
  derived dream artifact `mem_ba0c35995e20434f9097d313a5d77b97`
  (`dream:failure_pattern`, `confidence_score = 0.35`, `evidence_count = 1`)
  records the observation verbatim as
  `"[success] Investigate low-confidence dream finding: skill (plan_decomposed)"`.

- **(2) Is `skill` a concrete named defect or a generic placeholder label?** A
  generic placeholder. In `src/mac/dream_cycle_classifier.py:103` the first
  `_SKILL_PATTERNS` entry `(r"\bskill[s]?\b", "skill")` maps any occurrence of
  the bare word "skill"/"skills" to the canonical `area_name = "skill"`. The
  candidate summary literally contains the token
  ("...dream finding: **skill**..."), so the label is a self-fulfilling
  word-boundary match, not a named skill. `affected_tools`, `affected_providers`,
  and `affected_repo_areas` are all empty. The two live skill files
  (`skills/mac-agent-terminal-timeout/SKILL.md`,
  `skills/setup-mac-fleet/SKILL.md`) are healthy with valid frontmatter and
  coherent bodies.

- **(3) Single self-referential low-confidence record, or corroborated fault?**
  A single self-referential record with zero net-new support. Per the triage,
  the finding's sole evidence is the prior identical task's `plan_decomposed`
  success, and that chain repeats:
  `dreamrepair:828e1ef4...` ← `task_5568db61...` ← `mem_9c807de9...`;
  `task_5568db61...` → `dreamrepair:6b7c8892...` ← `task_70007fd6...` ←
  `mem_5c221c62...`; `task_70007fd6...` → `dreamrepair:cf8fa7ce...` ←
  `task_8764c80c...` ← `mem_22ba025a...`. All share the identical title
  "Investigate low-confidence dream finding: skill" and the identical
  "Supported by 1 memory record(s): [success] ... (plan_decomposed)" summary
  shape. This is a classifier feedback loop: the pattern re-derives itself from
  its own prior decomposition. Confidence `0.35` is the deterministic
  single-record structural floor for `support < 2`, not a diagnosed fault.

## Smallest repair vs. follow-up plan

Neither a repair nor a concrete follow-up is warranted, because no named defect
exists. There is no failing suite, assertion, stack trace, or reproducer tied to
this finding to repair, and both live `SKILL.md` files are healthy. Editing a
healthy skill on the basis of a non-defect is explicitly out of scope. The only
warranted artifact is this disposition note recording the closure and the
evidence gap.

## Recorded evidence gap

Support is effectively **zero net-new**: a single low-confidence (`0.35`,
`evidence_count = 1`), self-referential record that is a `plan_decomposed`
**success** (`outcome = "success"`, empty `error_signature`, `returncode = 0`),
tagged by a generic `\bskill[s]?\b` regex with no named
skill/tool/provider/repo-area and no reproducible failure signature. Confidence
`0.35` reflects `support < 2`, not a diagnosed fault.

## Reopen criteria

Reopen only if a *named* skill or tool acquires a reproducible failure signature
(a real error, stack trace, or failing test) supported by at least two
independent, non-self-referential evidence records — i.e. evidence that is not
itself the `plan_decomposed` success of a prior identical investigation task.

## Verification performed

- Consumed the dependency triage `docs/dream-triage-828e1ef4.md` and the sibling
  audits `findings.md` / `probe-skill-finding.md`; all three independently reach
  NOT ACTIONABLE.
- Confirmed the generic-label mechanism at first hand in
  `src/mac/dream_cycle_classifier.py:103` (`(r"\bskill[s]?\b", "skill")`).
- Confirmed the repository skill surface is two healthy files and that the task
  worktree is clean before this disposition note.
- Made **no** change to any `src/`, `tests/`, `skills/`, or `deploy/` file; the
  only change is this disposition note plus the regenerated documentation
  inventory that indexes it.
