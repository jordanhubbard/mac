# Investigation Conclusion: Low-Confidence "slack" Dream Finding `dreamrepair:d127812e01d62630fa16797756580276`

Read-only, investigation-only ground-truth record for parent audit task
`task_b04d1726fa6e4486b8a5154d0037d356` (title: "Investigate low-confidence dream
finding: slack"), plan node `ground_truth`. This document establishes ground
truth only. It changes **no** `src/`, `tests/`, `skills/`, or `deploy/` behaviour
and performs no skill, tool, or provider repair. Ground truth is drawn from the
reachable hub tasks/memory API (via the gateway bearer key) and read-only
inspection of the repository dream-classifier source.

## Summary Verdict

**NOT ACTIONABLE as a slack repair — the `slack` provider label is incidental,
not causal.** The finding is a low-confidence (0.35, `evidence_count=1`)
`failure_pattern` whose single evidence record is the *outcome/closure memory of a
prior investigation task that failed at executor startup* (an environment failure,
not a slack fault). The sole classification signal is the word-boundary regex
`\bslack\b` matching the literal word "slack" inside that prior task's own title.
There is no reproducible slack-provider defect, no error signature, and no code,
skill, or tool target. The correct disposition is to close the finding as NOT
ACTIONABLE for a slack repair.

## 1. The Finding's Claim (from the parent's `metadata.dream_repair`)

- fingerprint: `dreamrepair:d127812e01d62630fa16797756580276`
- kind: `failure_pattern`; scope: project `mac`
- affected providers: `["slack"]`; affected skills/tools/repo_areas: `[]` (empty)
- overall confidence: `low` (`overall_confidence_score = 0.35`)
- evidence_count: `1`
- classification signal: `\bslack\b` (a plain word-boundary token match)
- candidate summary: "failure pattern for task=`task_47c7e6938bcb4e7797745f4394486244`
  project=mac. Supported by 1 memory record(s): [failure] Investigate
  low-confidence slack dream finding: establish ground truth (repo_change)"

Every discriminating field is empty or a bare label. The only "signal" is the
English word "slack" matched by `\bslack\b`, and it matches the word "slack"
appearing in the **prior task's own title** — not a diagnosed slack fault. A
`failure_pattern` with support = 1 and a single bare-token signal is exactly the
shape the classifier scores lowest; 0.35 reflects support < 2 and one signal type,
not a diagnosed defect.

## 2. The Single Supporting Evidence Record

- record: `mem_c052b78e73dc46eca1c9761fe10f6a84`, `record_type
  deployment_learning:mac`, `subject project/mac`, created `2026-07-28T19:08:09Z`.
- Provenance: it is the **auto-generated outcome/closure memory of prior task**
  `task_47c7e6938bcb4e7797745f4394486244` ("Investigate low-confidence slack dream
  finding: establish ground truth"), also carried in that task's
  `metadata.salvage.recorded_lessons`.
- Full content (schema `mac.deployment_learning.v1`):
  `outcome=failure`, `evidence_type=repo_change`, `error_signature=""` (empty),
  `signals={checks_pass:null, files_changed:null, pushed:null, returncode:1,
  tests:null}`. The only non-null signal is `returncode=1`.

This record is **not an observation of a broken slack integration**. It is the
closure record of a previous investigation task that itself never completed. It
carries no error signature, no failing test, no stack trace, and no slack-specific
detail whatsoever. Treating this closure memory as fresh slack-failure evidence
adds zero net-new signal about any real defect.

## 3. What Actually Failed in the Prior Task `task_47c7e6938...`

The prior task did **not** fail because of any slack behaviour. From its hub task
record:

- `state = failed`; `metadata.failure_class = "environment"`.
- Activity trail: the assigned worker agent transitioned straight to a `diagnosis`
  phase with `failure = executor_failed` / problem "Task blocked:
  executor_failed", then the dispatcher recorded `non_retryable_attempt_failure`.
- `output_tail_unavailable_reason`: "transition supplied no stdout, stderr,
  output, log, or tail field" — i.e. **no work output was ever captured**; the
  attempt failed at executor startup.
- `repository_ref_lifecycle.disposition = failed_attempt`,
  `reason = non_retryable_attempt_failure`, `status = quarantined`.

So the prior task failed as an **environment/executor startup failure** before any
substantive slack investigation ran. Its own charter (fingerprint
`dreamrepair:aa55c5df3ef1db4140046162ad36a7b9`, evidence `mem_3ef89e15...`) was to
establish the same ground truth; it never got the chance to. `returncode=1` in the
evidence record reflects that non-zero executor exit, not a slack error.

## 4. The "slack" Provider Label Is Incidental Keyword-Match, Not Causal

The classifier that produced this finding is `src/mac/dream_cycle_classifier.py`.
Confirmed by direct read:

- `_PROVIDER_PATTERNS` contains `(r"\bslack\b", "slack")`.
- `_combined_text(candidate)` builds the match string from the candidate's
  `summary`, `kind`, `scope`, `observations`, `record_type_counts` keys, and
  retrieval `query_terms` — i.e. from **text about the prior task, not from any
  slack telemetry**.
- The dream artifact `mem_c3ad406b602b4ec0815b6bd2b0965027`
  (`dream:failure_pattern`) carries observation
  `"[failure] Investigate low-confidence slack dream finding: establish ground
  truth (repo_change)"` and the identical summary. The word "slack" in that title
  is the only thing `\bslack\b` matches.

Therefore the `slack` provider label is a **false-positive keyword match on the
task title**, not a causal attribution. No slack request, webhook, notifier call,
or configuration is implicated anywhere in the evidence. (For completeness, the
repository does contain a real slack notification surface — e.g.
`src/mac/notifier_service.py`, `src/mac/communication_service.py`,
`src/mac/_hermes/hermes_cli/slack_cli.py` — but **none of it is referenced by this
finding**; the finding names no file, skill, or tool.)

## 5. Whether the Prior Task Resolved or Superseded This Finding

- The prior task did **not** resolve the slack question: it failed at executor
  startup and produced no ground-truth write-up.
- It did **not** supersede this finding either. Instead it *manufactured* it: its
  failure closure record (`mem_c052b78e...`) became the sole evidence, and the
  next nap/dream cycle (`nap_5447516db547...`, run by a fleet worker agent)
  re-derived a new `failure_pattern` under a **distinct** fingerprint
  (`d127812e...`, vs. the prior charter's `aa55c5df...`). This is the same
  self-referential dream-repair loop documented in sibling investigations
  (e.g. `investigation-slack-finding-6349fdff.md`): each generation's only
  evidence is the previous generation's own outcome memory, and each recurrence
  gets a fresh fingerprint so dedup cannot suppress it.

## 6. Evidence Gaps (Explicit)

- **Single, self-referential record:** support is exactly one, and that record is
  the prior task's own failure-closure memory — effectively zero independent
  signal.
- **Empty error signature:** `error_signature=""`; the only non-null signal is
  `returncode=1`. There is no stack trace, no failing test, no reproducible
  slack error to characterize.
- **No independent corroboration:** no second, independent observation of a real
  slack failure exists.
- **Direct memory-fetch path not served:** `mem_c052b78e...` is not retrievable
  via `GET /memory/<id>`, `/memories/<id>`, or `/mac/memory/<id>` (all HTTP 404)
  while hub `/health` returns 200; it is reachable only through the task-scoped
  list `GET /memory?task_id=task_47c7e6938...`. The 404 is "path/record not
  served," not a downed service.

To raise confidence above `low`, the finding would need at least a second
independent, non-self-referential evidence record describing a concrete slack
failure — a real error signature, stack trace, or failing test naming a slack
skill/tool/code path.

## 7. Disposition and Reopen Criteria

- **Decision:** NOT ACTIONABLE as a slack repair. Close finding
  `dreamrepair:d127812e01d62630fa16797756580276` as such. No `src/`, `tests/`,
  `skills/`, or `deploy/` change is warranted for the slack surface; this note is
  the only artifact.
- **Underlying actionable defect is upstream in the dream-repair pipeline**, not
  in any slack provider: the loop treats a prior task's own failure-closure memory
  as fresh evidence and emits each recurrence under a new fingerprint. Remediation
  belongs to the pipeline/process, out of scope for this ground-truth task.
- **Reopen criteria (slack track only):** reopen only if the slack provider
  acquires a reproducible failure signature backed by at least two independent,
  non-self-referential evidence records.

## 8. Verification Performed

- Fetched parent `task_b04d1726...` and read `metadata.dream_repair`: provider
  `slack`, signal `\bslack\b`, support 1, confidence 0.35, evidence
  `mem_c052b78e...`, candidate task `task_47c7e6938...`.
- Fetched prior task `task_47c7e6938...`: `state=failed`,
  `failure_class=environment`, `executor_failed` / `non_retryable_attempt_failure`
  with no captured output; `salvage.recorded_lessons = [mem_c052b78e...]`.
- Retrieved and read the evidence record `mem_c052b78e...` via
  `GET /memory?task_id=task_47c7e6938...`: `deployment_learning:mac`,
  `outcome=failure`, empty `error_signature`, `returncode=1`, all other signals
  null. Also read the sibling `nap_summary` and `dream:failure_pattern` records
  from the same task group confirming the self-referential derivation.
- Read `src/mac/dream_cycle_classifier.py` and confirmed `\bslack\b` is a
  provider pattern matched against candidate summary/observations text (the prior
  task title), establishing the label is incidental keyword-match, not causal.
- Confirmed the finding names no skill, tool, repo area, or file, and that the
  repository's real slack notifier surface is not referenced by the finding.
- Probed the hub memory REST API for `mem_c052b78e...`: HTTP 404 on direct
  `/memory/<id>` paths while `/health` returned 200; record retrievable only via
  the task-scoped list endpoint.
- No `src/`, `tests/`, `skills/`, or `deploy/` file was edited. Fleet-generic: no
  secrets, hostnames, personal paths, or operator identities are recorded.
