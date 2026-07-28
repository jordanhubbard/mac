!!! warning "Historical field note"
    This record preserves ground-truth investigation evidence for a single dream
    finding. It is not a current operating contract; use the numbered book and
    current runbooks for instructions.

# Ground Truth: dream finding `dreamrepair:394db89d377ef58abf97ace7d54d728c` (slack failure_pattern)

**Task**: `task_7df391b62f8a4232b2c604f4a6e91acb` — read-only, investigation-only
audit for parent `task_c3f26a52452648a6994afb50257af219` ("Investigate
low-confidence dream finding: slack"), plan node `investigation`. This document
records ground truth only. It changes **no** `src/`, `tests/`, `skills/`, or
`deploy/` file and makes no skill, tool, or provider repair.

**Prepared by**: fleet worker (investigation node; no production code, test,
skill, config, or deploy edits).

## Verdict

**NOT ACTIONABLE as a slack repair — evidence gap, no real slack-provider
failure exists. The signal is noise from a single, self-referential one-off
record.**

## (a) Is the finding actionable? — No

There is no concrete, reproducible defect in any slack provider, skill, or tool
that a code or skill change would fix. The finding is a low-confidence
(`0.35`, `evidence_count=1`) `failure_pattern` whose only target is the bare
provider label `slack`, whose sole evidence record is the auto-generated failure
outcome of a *source task that itself failed for environment reasons and never
performed an investigation*, and whose lineage is a self-referential feedback
loop 15 generations deep.

## (b) Concrete evidence supporting that call

### The finding's claim (from `dream_repair.classification`)
- fingerprint: `dreamrepair:394db89d377ef58abf97ace7d54d728c`
- kind: `failure_pattern`; scope: project `mac`
- confidence: `low` (`overall_confidence_score = 0.35`); `evidence_count = 1`
- affected: providers `["slack"]`; skills, tools, repo_areas all empty
- signal: `\bslack\b` — a plain word-boundary regex on the token "slack"

Every discriminating field is empty or a generic label. `0.35` reflects
support `< 2`, not a diagnosed fault.

### The single supporting record is a failed task's own outcome memory
- record: `mem_6d53574e86a548c2a0658f33c43641cb`
  (`record_type = deployment_learning:mac`, created by `mac-task-executor`).
- content (verbatim fields): `outcome = "failure"`,
  `evidence_type = "investigation"`, `error_signature = ""` (empty),
  `repository = ""`, `signals = {returncode: 1, tests: null, checks_pass: null,
  files_changed: null, pushed: null}`,
  `task_id = task_0836b4b1c802465a93e3bd6a1affcfc0`,
  `task_title = "Investigate low-confidence dream finding: slack"`.

This is the executor's auto-emitted closure record for the source task, not an
observation of a broken slack integration. It carries an **empty error
signature** and no slack-specific detail.

### The source task never investigated anything
- source task `task_0836b4b1c802465a93e3bd6a1affcfc0`: `state = failed`,
  `failure_class = environment`, activity shows `executor_failed` /
  `non_retryable_attempt_failure` at sandbox startup.
- Its `salvage.recorded_lessons` is exactly `[mem_6d53574e86a548c2a0658f33c43641cb]`
  — i.e., this finding's sole evidence is the salvage/outcome memory of a task
  that died in its environment before doing any slack work.

### The finding was minted by the nap/dream loop from that one record
- The `dream:failure_pattern` record `mem_8e0638d1f9774af3b25ca53f6aeaa727`
  (created by `nap-ticker`) has a single evidence pointer to
  `mem_6d53574e...` and its only `observations` entry is the literal string
  `"[failure] Investigate low-confidence dream finding: slack (investigation)"`.
  The provider label `slack` is `\bslack\b` matching the word "slack" in the
  prior task's own **title**, not a diagnosed slack fault.
- `slack` is a known provider token the classifier recognizes
  (`src/mac/dream_cycle_classifier.py`), so any title containing the word gets
  the label regardless of substance.

### The lineage is self-referential, 15 generations deep
Walking each generation's `dream_repair.candidate.task_id` upward:

```
task_0836b4b1  failed/environment  394db89d… <-- this finding's source task
  <- task_183affd  failed/environment
    <- task_648ccf21 failed/environment
      <- task_68e838c5 failed/environment
        <- task_48e57996 failed/environment
          <- task_ec90bcd9 failed/environment
            <- task_14c901cd failed/environment
              <- task_7485257c failed/environment
                <- task_77121bb1 failed/environment
                  <- task_13edcff7 failed/environment
                    <- task_3f4a754d failed/environment
                      <- task_26953e7c failed/environment
                        <- task_c7e109f6 failed/environment
                          <- task_46bd1a84 cancelled
                            <- task_027ac881 failed/work (root audit)
```

Every ancestor is `failed` (mostly `failure_class=environment`) or `cancelled`;
every generation carries a DISTINCT fingerprint, so fingerprint dedup never
suppresses the recurrence. Each generation's only evidence is the previous
generation's own `deployment_learning` outcome memory — zero net-new signal.

### The root audit already reached this conclusion
- root task `task_027ac881b6914677b9bf3aa704d3cc91` (`failure_class = work`)
  actually performed the audit and recorded: the finding is NOT ACTIONABLE, the
  single evidence was a success outcome (a `plan_decomposed` task), and `slack`
  is a bare regex token — not a diagnosed fault. That task failed only on the
  repository push/contract gate, not on the substance of its audit.

### No slack surface is implicated
- The finding names no file, tool, skill, or error signature. A read-only scan of
  `src/`, `skills/`, and `deploy/` finds a messaging-platform slack binding in the
  Hermes gateway surface, but nothing in the finding points at it and there is no
  reproducible failure, stack trace, or failing test against it. The task worktree
  is clean.

## (c) The specific evidence gap (why not actionable)

- **Single, self-referential record**: support is one record, and that record is
  the source task's own failure-outcome memory — net-new independent signal is
  zero.
- **Empty error signature**: `mem_6d53574e...` carries `error_signature = ""`
  and `returncode = 1` with no slack-specific detail; there is nothing to
  reproduce or repair.
- **Environment failure, not a slack failure**: the source task failed at sandbox
  startup (`failure_class = environment`, `executor_failed`); it never exercised
  any slack path.
- **Label is a title regex match**: `slack` comes from `\bslack\b` matching the
  word "slack" in the prior task title, not from a diagnosed provider fault; all
  skill/tool/repo_area fields are empty.
- **No independent corroboration**: every generation reuses the previous
  generation's memory; there is no second, independent observation of a real
  slack failure.
- **Sole record not retrievable by id**: direct hub fetches for
  `mem_6d53574e...` at `/memory/<id>`, `/memories/<id>`, and `/mac/memory/<id>`
  return HTTP 404 while `/health` returns 200 (the record is reachable only via
  the `task_id`-scoped `/memory` search, not a not-found service).

## Disposition and reopen criteria

- **Decision**: NOT ACTIONABLE as a slack repair. Close finding
  `dreamrepair:394db89d377ef58abf97ace7d54d728c` as such.
- **Genuine underlying issue is upstream in the dream-repair pipeline**, not in
  any slack provider/skill/tool: the loop treats a failed task's own outcome
  memory as fresh failure evidence and emits each recurrence under a distinct
  fingerprint, so dedup cannot suppress the chain. Remediation belongs to the
  pipeline/process and is out of scope for this investigation-only task.
- **Reopen (slack track only)** if the slack surface acquires a reproducible
  failure signature (a real error, stack trace, or failing test) backed by at
  least two independent, non-self-referential evidence records.

## Verification performed

- Retrieved source `task_0836b4b1...` and grandparent `task_183affd...` via the
  hub tasks API; confirmed `state=failed`, `failure_class=environment`, and
  `salvage.recorded_lessons = [mem_6d53574e...]`.
- Fetched `mem_6d53574e...` via `/memory?task_id=task_0836b4b1...`; confirmed
  `record_type=deployment_learning:mac`, `outcome=failure`,
  `error_signature=""`, created by `mac-task-executor`.
- Identified the minting `dream:failure_pattern` record `mem_8e0638d1...`
  (by `nap-ticker`); confirmed its single evidence pointer and title-derived
  observation string.
- Walked 15 generations of `candidate.task_id` ancestry to the root audit
  `task_027ac881...`; confirmed all `failed`/`cancelled`, distinct fingerprints,
  and that the root audit already found the finding NOT ACTIONABLE.
- Confirmed no slack provider/skill/tool is named by the finding and the worktree
  is clean. No `src/`, `tests/`, `skills/`, or `deploy/` file was edited.
  Fleet-generic: no secrets, hostnames, personal paths, or operator identities
  are recorded.
