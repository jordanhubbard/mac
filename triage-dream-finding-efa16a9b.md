# Triage: Low-Confidence Dream Finding `dreamrepair:efa16a9b413bc11727b3dbe6103ecc91` (skill, tool)

Read-only, investigation-only triage for plan node `triage` of the parent audit
"Investigate low-confidence dream finding: skill, tool". This note records
ground truth only. It changes **no** `src/`, `tests/`, `skills/`, `deploy/`,
tool, or provider code and performs no repair. Output is fleet-generic: no
secrets, host names, personal paths, or operator identities. Scope is exactly
the task contract — inspect the single supporting record and the origin
candidate, decide whether the `skill`/`tool` labels are genuine or generic-word
noise, and state whether one low-confidence record is an actionable failure
pattern.

## Verdict

**actionable = no (preliminary).** The finding is a low-confidence
(`overall_confidence_score = 0.35`, `evidence_count = 1`) `failure_pattern` whose
only labels are the generic tokens `skill` and `tool`. Both labels are
word-boundary regex matches echoing the vocabulary of the sole supporting
record, not named skills or tools. That single record is itself a **failed
read-only code-inventory task about the label-extraction code path**, so the
finding re-derives itself from its own subject matter and carries zero net-new,
independent evidence of a defect. There is no reproducible failure signature for
a code, skill, or tool change to fix.

## Inputs Triaged (as given by the task contract)

- **Finding:** fingerprint `dreamrepair:efa16a9b413bc11727b3dbe6103ecc91`, kind
  `failure_pattern`, scope project `mac`, confidence `low`
  (`overall_confidence_score = 0.35`), `evidence_count = 1`.
- **Affected labels:** Skills = `skill`, Tools = `tool`. No named skill asset,
  tool id, provider, or repo area is attached beyond those two bare tokens.
- **Classifier signals:** `\bskill[s]?\b` and `\btool\b` — plain word-boundary
  token matches.
- **Sole supporting record:** `mem_9d1839bd170a4c79856cfcdf2162fca0`,
  `record_type = deployment_learning:mac`, from originating task
  `task_4ee922c0388146de940a4ba70f28db4f`. Per the task contract this originating
  task is a FAILED read-only code inventory task ABOUT the label-extraction code
  path — i.e. a task whose own text is saturated with the words "skill" and
  "tool".

## 1. The Single Supporting Record

`mem_9d1839bd170a4c79856cfcdf2162fca0` (`deployment_learning:mac`) is the
outcome memory of `task_4ee922c0388146de940a4ba70f28db4f`, a read-only code
inventory of the label-extraction path that ended in failure. As an inventory
task about labels, its title / summary / error text necessarily contain the
generic terms "skill" and "tool". It carries no failing test, stack trace, or
error signature that names a defect in any concrete skill or tool. A single
`deployment_learning:mac` record yields the deterministic low tier (`0.35`)
because support is `< 2`; the score encodes insufficient evidence, not a
diagnosed fault.

## 2. Label Provenance — Generic-Word Noise, Not Genuine Named Skills/Tools

Both `skill` and `tool` are generic-word matches, not genuine named
skills/tools:

- The affected labels are produced by bare word-boundary regexes. Prior in-repo
  field notes on identical findings document the legacy mechanism: a
  `_SKILL_PATTERNS` entry `(r"\bskill[s]?\b", "skill")` and the analogous
  `\btool\b` → `tool` mapping collapse any occurrence of the words
  "skill"/"skills"/"tool" to the canonical area names `skill` / `tool` (see
  `docs/disposition-task-c3a30819-skill.md` and
  `docs/archive/field-notes/investigation-dream-skill-tool_or_skill_name-actionability.md`).
- The sole evidence record is an inventory task ABOUT the label-extraction code
  path, so its own text is guaranteed to contain "skill" and "tool". The labels
  therefore **echo the record's own vocabulary** — a self-fulfilling
  word-boundary match — rather than pointing at a specific skill asset or tool
  binding.
- The MAC skill surface in this worktree is exactly two healthy files
  (`skills/mac-agent-terminal-timeout/SKILL.md`, `skills/setup-mac-fleet/SKILL.md`),
  neither of which is named or implicated by the finding. No tool id, provider,
  or repo area is attached.

**Finding:** `skill` and `tool` are generic-word noise (classifier placeholders),
not genuine named skills or tools.

## 3. Is One Low-Confidence Record an Actionable Failure Pattern?

No. A "failure pattern" requires a recurring, reproducible failure with a named
target. This finding has:

- **No named target** — only the generic tokens `skill` and `tool`.
- **No reproducible failure** — the signals are bare word-boundary matches on
  task text, not an error signature, stack trace, or failing test.
- **No independent support** — a single `evidence_count = 1` record that is
  itself about the label-extraction path, so the finding is self-referential and
  adds zero net-new evidence.
- **Low by construction** — `0.35` is the deterministic single-record floor for
  support `< 2`, which encodes insufficient evidence, not a diagnosed defect.

A single low-confidence, self-referential inventory record does **not**
constitute an actionable failure pattern.

## 4. Specific Evidence Gap (why not actionable)

Support is effectively **zero net-new**: one low-confidence (`0.35`,
`evidence_count = 1`) `deployment_learning:mac` record that is the FAILED
outcome of a read-only code-inventory task ABOUT the label-extraction path;
`skill`/`tool` labels that are bare `\bskill[s]?\b` / `\btool\b` word-boundary
matches echoing that record's own vocabulary, with no named skill asset, tool
id, provider, or repo area; and no reproducible failure signature (no error,
stack trace, or failing test). To become actionable the finding needs a *named*
skill or tool with a reproducible failure signature backed by at least two
independent, non-self-referential evidence records.

## 5. Verification Performed (this node)

- Read the finding fields and the sole supporting record identity from the task
  contract (`metadata`); recorded them above.
- Corroborated the generic-label mechanism against in-repo field notes on
  identical `skill`/`tool` findings (`docs/disposition-task-c3a30819-skill.md`,
  `docs/archive/field-notes/investigation-dream-skill-tool_or_skill_name-actionability.md`)
  and against the current pipeline note `docs/dreaming-rewrite.md`.
- Confirmed the MAC skill surface is two healthy `SKILL.md` files and that the
  finding names none of them; no tool/provider/repo-area label is attached.
- Confirmed `git status --porcelain` on the task worktree is clean apart from
  this note; no `src/`, `tests/`, `skills/`, or `deploy/` file was modified.

## Assumptions Recorded

- The finding fields, the supporting-record identity
  (`mem_9d1839bd170a4c79856cfcdf2162fca0`), the originating task
  (`task_4ee922c0388146de940a4ba70f28db4f`), and the `evidence_count = 1`
  carried in the task contract are authoritative for the finding's content. The
  reachable `MAC_HUB_URL` endpoint is an OpenAI-compatible LLM gateway (`/v1`),
  not a memory/tasks read API, so the record could not be directly dereferenced
  from this sandbox; ground truth was established from the task metadata plus
  in-repo corroboration.
- The legacy `dream_scanner` / `dream_cycle_classifier` / `dream_repair_tasks`
  modules that mint these labels are not present in this checkout (the dreaming
  pipeline was rewritten into `src/mac/dreaming/`; see `docs/dreaming-rewrite.md`).
  The label mechanism is therefore cited from prior in-repo field notes rather
  than re-executed here.
- `skill` and `tool` in the finding are the classifier's generic area
  placeholders, not diagnosed faults in any specific skill or tool.

## Handoff to the Parent Audit

- **Decision:** actionable = no (preliminary); close the finding as
  NOT ACTIONABLE — generic-word noise, self-referential single-record evidence.
- **Label provenance:** `skill` and `tool` are generic `\bskill[s]?\b` /
  `\btool\b` word-boundary matches echoing the supporting record's own
  vocabulary — not genuine named skills or tools.
- **Reopen criteria:** reopen only if a *named* skill or tool acquires a
  reproducible failure signature (a real error, stack trace, or failing test)
  supported by at least two independent, non-self-referential evidence records.
- **Out-of-scope upstream note:** the durable improvement is a pipeline change —
  stop minting low-confidence repair findings from single self-referential
  inventory records via bare word-boundary matches. That is pipeline-scoped, not
  a skill/tool repair, and is not implemented by this investigation-only task.
