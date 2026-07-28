# Loop Analysis: Why the Low-Confidence "slack" Dream Finding Keeps Regenerating

Read-only, investigation-only analysis for plan node `loop_analysis` of parent
audit `task_b5f1996e04c54942b27d7cdbe40597b5` (title: "Investigate low-confidence
dream finding: slack"), building on the `confirm_ground_truth` closure in
`investigation-slack-finding-6349fdff.md`. This document records ground truth
about the pipeline mechanism only. It changes **no** `src/`, `tests/`,
`skills/`, `deploy/`, tool, or provider file, and makes no skill or tool edit.
Fleet-generic: no secrets, hostnames, personal paths, or operator identities.

## Summary Verdict

The "slack" finding is not a slack defect; it is a **self-perpetuating feedback
loop in the dream-repair pipeline**. Each generation manufactures the next from
its own prior *failure outcome memory*. The loop never terminates on its own
because (a) the only dedup key — a per-finding fingerprint — is computed from
text that embeds the *previous* generation's task id, so every generation gets a
NEW fingerprint; and (b) each investigation task fails in one of two ways that
both emit a fresh `deployment_learning` outcome memory whose title still contains
the word "slack", re-seeding the next dream cycle. The correct fix is upstream in
the pipeline (break the self-reference), not in any slack integration.

## 1. Causal Diagram of the Loop

```
                 ┌──────────────────────────────────────────────────────────┐
                 │                                                          │
                 v                                                          │
 (A) Investigation task titled "...dream finding: slack" FAILS              │
      │   two distinct failure modes — see §4                              │
      v                                                                    │
 (B) Executor/finalizer writes a deployment_learning:mac memory            │
      build_learning_record() stamps task_title verbatim:                  │
      "Investigate low-confidence dream finding: slack"                    │
      (src/mac/executor_memory.py:362, record_deployment_learning)         │
      │                                                                    │
      v                                                                    │
 (C) Next nap/dream cycle groups memories by task and dreams a candidate   │
      _record_observation() renders the record as                          │
      "[failed] Investigate low-confidence dream finding: slack            │
       (investigation)"  (src/mac/nap_consolidator.py:113)                 │
      _default_dreamer() builds summary                                    │
      "failure pattern for task=<PRIOR_ID>. Supported by 1 memory          │
       record(s): [failed] ...slack... " ; confidence=low (support=1)      │
      (src/mac/nap_consolidator.py:162, :179)                              │
      │                                                                    │
      v                                                                    │
 (D) classify_candidate() matches regex \bslack\b in that summary          │
      -> provider area "slack", confidence low, score 0.35, evidence 1     │
      (src/mac/dream_cycle_classifier.py:132 _PROVIDER_PATTERNS,           │
       "\\bslack\\b" -> "slack")                                           │
      │                                                                    │
      v                                                                    │
 (E) file_low_confidence_repair_tasks() gate: overall_confidence==low      │
      AND affected provider present -> mint a NEW investigation task       │
      repair_fingerprint() hashes summary (which embeds <PRIOR_ID>)        │
      -> a NEW fingerprint each generation                                 │
      (src/mac/dream_repair_tasks.py:102, :175, :189)                      │
      │                                                                    │
      └────────────────> new task titled "...dream finding: slack" ────────┘
                          (loops back to A)
```

The single "signal" throughout is the English word **slack** carried in the
prior task's own title; no generation ever observes a real slack integration
failure.

## 2. The Self-Reference Injection Point

- Failure closure writes the memory: `record_deployment_learning` posts a
  `mac.deployment_learning.v1` blob whose `content` includes `task_title`
  verbatim (`src/mac/executor_memory.py:362`). For this lineage the title is
  always "Investigate low-confidence dream finding: slack".
- The dreamer reads that memory back: `_record_observation` special-cases the
  `mac.deployment_learning.v1` schema and renders
  `"[<outcome>] <task_title> (<evidence_type>)"`
  (`src/mac/nap_consolidator.py:113`). The word "slack" is thereby reinjected
  into the dream artifact's `summary` and `observations`.
- `_default_dreamer` sets `confidence=low` whenever support is a single record
  (`_confidence_for_records`, support < 2 -> low ≈ 0.35;
  `src/mac/nap_consolidator.py:153`). A lone self-referential outcome memory is
  exactly one record, so every generation is low-confidence by construction.

## 3. Fingerprints Differ Per Generation (dedup is ineffective)

`repair_fingerprint` builds its dedup key from material that includes
`summary` and the candidate `signature`
(`src/mac/dream_repair_tasks.py:175`, key `"summary"` at :189). The dreamer's
`summary` begins with `"... for task=<PRIOR_TASK_ID>. Supported by 1 memory
record(s): ..."` (group label `task=<id>` from
`src/mac/nap_consolidator.py:495`). Because `<PRIOR_TASK_ID>` changes every
generation, the SHA-256 over the material changes too, so
`_existing_repair_fingerprints` (`src/mac/dream_repair_tasks.py:196`) never finds
a match and the "already filed" dedup branch (`:127`) is never taken.

Reproduction with the repository's own code (two consecutive generations, same
shape, different prior task id):

```
prior=task_16d2505ae36a45cfb37ec96a473587ad
  overall=low score=0.35 evidence=1  provider=slack  signal=\bslack\b
  fingerprint=dreamrepair:845cccdcb7c92bdb9a322745dd8c189d
prior=task_e16840bb5f3d4a8eae15d35e67dbd76c
  overall=low score=0.35 evidence=1  provider=slack  signal=\bslack\b
  fingerprint=dreamrepair:9b5e448b7fca45d55452f13d1da3cda3
```

Identical classification (provider `slack`, confidence low 0.35, one evidence
record, signal `\bslack\b`) but two DIFFERENT fingerprints. This matches the
distinct fingerprints observed across the live lineage in
`investigation-slack-finding-6349fdff.md` §4 (e.g. `6349fdff...`). Fingerprint-
based suppression therefore cannot stop regeneration: it only ever suppresses a
literal re-file of the exact same `<PRIOR_TASK_ID>` summary, which the pipeline
never produces twice.

Per-cycle spawn caps (`MAX_TASKS_PER_CYCLE`,
`src/mac/dream_repair_tasks.py:33`) bound the *burst width* of a single tick but
not the *depth* of the chain over time, so they do not break the loop either.

## 4. The Two Distinct Failure Modes That Feed the Loop

Both modes end the same way: the task reaches a `failed` outcome, and the
finalizer writes a `deployment_learning:mac` memory whose title contains "slack"
(step B). They differ in WHERE the failure occurs.

### Mode 1 — Infrastructure failure (executor_failed / non_retryable_attempt_failure)

The attempt never produces valid evidence because the environment or executor
itself fails. `attempt_failure_classifier` maps markers such as
`executor_failed`, `heartbeat_offline`, `lease_expired`, `authentication
failed`, `could not clone`, `command not found`, `network`/`connection`
conditions to the ENVIRONMENT class
(`src/mac/attempt_failure_classifier.py:237` and surrounding marker list). The
attempt is recorded as a non-retryable/exhausted failure; the finalizer still
runs `record_deployment_learning` on the (failed) outcome
(`src/mac/executor_finalizer.py:981`). Net effect: a `[failed] ...slack...`
memory is written even though NO substantive slack analysis ran — pure infra
noise that nonetheless re-seeds the dream cycle.

### Mode 2 — Contract-verification mismatch on a non-repository investigation

Here the worker DID the investigation and tried to submit
`evidence_type=investigation`, but the task carries a repository execution
contract (this very task: `execution_contract.evidence_type=repo_change`, with
`evidence.required` including `repo.head_sha`, `repo.pushed`). Verification
rejects it:

- `_verification_type_problems` requires that, for `evidence_type=investigation`,
  the task metadata declare a non-repository outcome via
  `declared_non_repository_outcome_evidence_type(task.metadata)` returning
  `"investigation"` (`src/mac/services.py:25085`,
  `src/mac/models.py:501`). When project registration has enriched the task with
  a `repository_contract`/`repo_change` contract and no explicit non-repository
  declaration survives, this returns `""`, so the gate emits
  "investigation evidence requires an operator-authored investigation execution
  contract" and fails the contract.
- If the worker instead submits `repo_change`, the `RepoChangeValidator`
  requires a pushed repo anchor — `repo.pushed=true` with `remote_ref`/`pr_url`
  and a valid `repo.head_sha` (`src/mac/evidence_validators.py:193`, :226). A
  read-only investigation produces no commit and nothing to push, so this fails
  too. `operator_result` is likewise rejected on a repo-coupled task
  (`src/mac/evidence_validators.py:395`).

Either way the task ends `failed` on a contract mismatch — a NON-repository
investigation held to repo-evidence/push requirements — and the finalizer again
writes a `[failed] ...slack...` outcome memory, feeding step B.

### How each mode feeds the loop

Both modes are indistinguishable to the downstream dreamer: it only reads the
resulting `deployment_learning` memory, whose `outcome=failed` and
`task_title` still literally contains "slack". `_dream_kind` sees "failed" and
labels the artifact `failure_pattern` (`src/mac/nap_consolidator.py:142`),
`\bslack\b` matches, confidence is low (support 1), and a new task is minted with
a new fingerprint. The *cause* of the failure is irrelevant to loop
continuation; the *existence* of any failure memory carrying the token "slack"
is sufficient.

## 5. Why Existing Guards Do Not Stop It

- Fingerprint dedup (`_existing_repair_fingerprints`): defeated because the
  fingerprint material embeds the prior task id (§3).
- Inventory-only skip (`DREAM_INVENTORY_ONLY_KINDS`,
  `src/mac/dream_repair_tasks.py:26`): only skips `tool_or_skill_name`
  candidates; the slack candidate is a `failure_pattern`, so it is not skipped.
- Confidence gate (`overall_confidence != "low"` -> skip,
  `src/mac/dream_repair_tasks.py:114`): the loop lives BELOW this gate — it fires
  precisely because the finding IS low-confidence with an affected provider.
- Per-cycle spawn budget: bounds burst width, not chain depth over successive
  ticks.

## 6. Where a Real Fix Belongs (analysis only — no change made here)

The defect is the self-reference, not slack. Durable options, in rough
preference order (for the disposition/owner to decide; this task does not
implement any):

1. Make the dreamer NOT treat a task's own prior `deployment_learning` outcome
   memory as fresh failure evidence for a new finding about that same task —
   i.e. exclude `deployment_learning`/`nap_summary` self-outcomes from the
   evidence set that seeds `failure_pattern` candidates
   (`_default_dreamer` / `_dream_kind`), the same way `nap_summary` rows are
   already excluded from re-summarization.
2. Make `repair_fingerprint` provenance-stable: derive the dedup key from the
   *finding's semantic target* (provider/area + signal) rather than from a
   `summary` that embeds a volatile prior task id, so successive generations of
   the identical finding collapse to one fingerprint and dedup works.
3. Ensure non-repository investigation tasks spawned from dream findings carry an
   operator-authored `evidence_type=investigation` contract so verification does
   not fail them on repo-evidence/push requirements (removing Mode 2 as a loop
   feeder).

Any one of (1)–(3) breaks the cycle; (1) or (2) is the most direct root-cause
fix because it stops the pipeline from manufacturing findings from its own
output regardless of why the prior task failed.

## 7. Verification Performed

- Read the classifier, dreamer, fingerprint, and verification code paths cited
  above (`dream_cycle_classifier.py`, `nap_consolidator.py`,
  `dream_repair_tasks.py`, `executor_memory.py`, `executor_finalizer.py`,
  `attempt_failure_classifier.py`, `evidence_validators.py`, `models.py`,
  `services.py`).
- Reproduced the fingerprint divergence and identical low/0.35 slack
  classification for two consecutive generations using the repository's own
  `classify_candidate` and `repair_fingerprint` (§3 output).
- Corroborated the observed lineage and per-generation distinct fingerprints
  documented in the sibling closure `investigation-slack-finding-6349fdff.md`.
- No `src/`, `tests/`, `skills/`, or `deploy/` file was edited; the analysis is
  the only artifact. Fleet-generic; no secrets, hostnames, personal paths, or
  operator identities recorded.
