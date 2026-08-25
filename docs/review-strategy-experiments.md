# Review-strategy experiments

MAC can measure whether an independent model contributes useful signal after
an executor model, rather than treating multi-model review as an axiom. The
task ledger is the source of truth: assignments and later outcome labels are
task metadata; executor evidence, signed review verdicts, task history, and
publication records are the observations. Reports are derived from that
ledger, so they can be replayed as the analysis changes and cannot drift into
a separate telemetry database.

This facility does not claim that a small run proves one strategy is better.
It makes the premise falsifiable and gives the system a conservative feedback
loop. A report refuses to recommend an arm until every arm has enough terminal
tasks, completed reviews, validated outcomes, and protocol-compliant passes.
Even then it emits a policy *candidate*, never an automatic production policy
change. Statistical confidence and operator promotion remain separate gates.

## What is measured

Each `mac.review_experiment.v1` assignment records the experiment, arm,
propensity, policy version, stratum, hypothesis, and assignment time. Weighted
assignment is deterministic from `(experiment_id, task_id, policy_version)`;
this makes a choice reproducible while retaining the probability needed for
later analysis. An assignment is immutable and must be made before a task is
claimed.

For controlled cross-model trials, set task metadata `model` for the executor
and `review_model` for the reviewer. Review payloads never inherit the executor
pin; when `review_model` is absent they use the review worker's fleet default.
Set `max_iterations` and `review_max_iterations` in task metadata to bound the
Hermes tool-calling budget per executor pass and per reviewer pass. Blind review
runs discovery and adjudication separately, so its worst-case reviewer call
count is twice `review_max_iterations`. These are task-local controls and do not
change the fleet default or substitute a smaller, unvalidated model.
The observation joins task-attributed `llm.route` records and reports the
resolved models, token counts, and latency actually used.

For every task, `mac.review_observation.v1` derives:

- executor and reviewer model identities, including model family/provider
  when declared or conservatively derived from the model id;
- the actual relationship (`same_model`, `cross_model`, `cross_provider`, or
  `cross_family`), instead of trusting the declared arm name;
- review verdicts, independent and final findings, protocol compliance,
  lifecycle completion, and pending reviews;
- usage fields when the model runner supplies them, plus blind-discovery
  duration; and
- operator labels and delayed outcomes, including confirmed/refuted findings,
  clean observation windows, and escaped defects.

The existing integrity rules still apply. A reviewer must be a different
agent/persona and an approval for model-generated executor evidence must name a
different model. Experimentation cannot weaken signature, test, publication,
or reviewer-eligibility gates.

## Evidence-withheld discovery

An assignment with `blind=true` uses two reviewer passes:

1. The host physically renames `executor-evidence.json` out of the workspace,
   invokes the reviewer against the task, checkout, diff, and focused checks,
   and requires `review-independent-findings.json`.
2. The host restores executor evidence and invokes the adjudication pass. The
   reviewer must preserve, refine, or explicitly rebut its independent
   findings while deciding the final verdict.

The host writes `review-protocol.json` with evidence-withholding status,
return code, duration, output hashes, findings-file hash/count, and a compliance
result. Both files are captured as durable evidence artifacts and the
authoritative protocol record is copied into the signed review verdict. A
missing or malformed discovery record does not fabricate a finding; it marks
the pass noncompliant and prevents that arm from becoming a policy candidate.

Use `--blind-arm NAME` with weighted assignment when only selected arms should
receive this treatment. `--blind` deliberately applies it to every possible
arm in that assignment.

## Operator workflow

Stage work before assigning it so the dispatcher cannot win the race:

```console
mac task create "Add parser boundary tests" --project=nanolang --no-dispatch

mac admin review experiment assign TASK_ID review-protocol-2026-07 \
  --arms standard=1,blind=1 \
  --blind-arm blind \
  --policy-version=v1 \
  --hypothesis-file=hypothesis.txt \
  --stratum=small-maintenance

mac task release TASK_ID
mac admin dispatch tick --limit 10
```

For a fixed arm, use `--arm standard` or `--arm blind --blind`. The explicit
assignment propensity defaults to 1; set `--probability` only when the arm was
selected by an external randomized policy whose propensity is known.

Inspect one derived lifecycle:

```console
mac admin review experiment observe TASK_ID
```

Review findings start unresolved. After reproduction or downstream
observation, append a durable label without altering the signed evidence:

```console
mac admin review experiment outcome TASK_ID finding_validation confirmed \
  --finding-id=FINDING_ID \
  --severity-weight=2 \
  --source=operator-reproduction \
  --detail-file=validation.json

mac admin review experiment outcome TASK_ID clean_window confirmed \
  --severity-weight=0 \
  --source=post-merge-ci \
  --detail-file=window.json

mac admin review experiment outcome TASK_ID escaped_defect confirmed \
  --severity-weight=4 \
  --source=incident \
  --detail-file=incident.json

mac admin review experiment outcome TASK_ID protocol_invalid confirmed \
  --finding-id=operator:blind-treatment-leak \
  --severity-weight=0 \
  --source=payload-audit \
  --detail-file=protocol-invalid.json
```

The accepted outcome states are `pending`, `confirmed`, and `refuted`.
`finding_validation`, `clean_window`, and `escaped_defect` are the outcome
kinds counted by the policy gate; other kinds remain visible but do not satisfy
the minimum validation threshold. A confirmed `protocol_invalid` outcome is a
special fail-closed signal: observations remain replayable, but the sample is
marked invalid and every associated review pass is counted non-compliant so it
cannot support a policy promotion.

Derive an experiment report:

```console
mac admin review experiment report review-protocol-2026-07 --project=nanolang
```

The default gate requires five terminal, reviewed tasks and three counted
outcomes in every arm. Lower thresholds are useful for plumbing validation,
but are not evidence of scientific superiority:

```console
mac admin review experiment report review-protocol-2026-07 \
  --project=nanolang \
  --min-tasks-per-arm=2 \
  --min-validated-outcomes-per-arm=1
```

## Ledger source map and extraction plan

Use the durable task ledger as the only source of truth. For SQLite, the JSON
examples below use `json_extract`; for Postgres, use the equivalent JSONB
operators on the same stored columns.

| Question | Durable sources | Existing code/docs/tests | Extraction |
| --- | --- | --- | --- |
| Reviewer relationship | `evidence.metadata.verification` for executor and `review_verdict` manifests; `reviews.reviewer_agent_id/evidence_id`; `evidence.created_by`; `leases.agent_id` for prior owners; `tasks.metadata.review_experiment`; `task_history.detail.reviewer_independence*` when fallback is used. | `src/mac/review_experiments.py` (`build_observation`, `_strategy`); `src/mac/review_service.py` (`cross_llm_review_problems`); `src/mac/services.py` reviewer eligibility helpers; `tests/test_review_experiments.py`, `tests/test_control_plane.py`; this document. | Join `reviews` to review verdict evidence, then follow `reviewed_evidence_id` back to executor evidence. Derive model/provider/family from both manifests and agent relationship from `created_by`, `reviewer_agent_id`, lease history, and review-request transition detail. |
| Rejection cause and `failure_class` | `reviews.status/reason`; review verdict manifest fields `verdict`, `semantic_verdict`, `feedback`, `findings`; `task_history.detail.reason/failure_class/problems`; terminal `tasks.metadata.failure_class/salvage`. | `src/mac/review_failure_classifier.py`; `src/mac/attempt_failure_classifier.py`; `src/mac/services.py` terminal attempt classification; `tests/test_review_failure_classifier.py`, `tests/test_attempt_failure_classifier.py`, `tests/test_review_protocol_rejection.py`. | Classify review retractions with `classify_review_failure`. For exhausted attempts, read the already-persisted `tasks.metadata.failure_class` and fall back to replaying `task_history` through `classify_attempt_failure` only for older rows. |
| Post-publication defect signals | `tasks.metadata.review_outcomes[]` with `kind` values `finding_validation`, `clean_window`, `escaped_defect`, `protocol_invalid`; `publications`; `scientific_observations.metrics.escaped_defect_severity/quality_source`; `task_history` event `task.review_outcome_recorded`. | `src/mac/review_experiments.py` (`build_outcome`, `append_outcome`); `src/mac/scientific_optimizer.py` (`derive_task_kpis`); `docs/scientific-optimizer.md`; `tests/test_review_experiments.py`, `tests/test_scientific_optimizer.py`. | Treat outcome labels as delayed annotations, not rewrites of signed evidence. Aggregate confirmed `escaped_defect` severity per task and join to publication rows to separate pre-publication rejection from post-publication quality failures. |
| Starvation episodes | No dedicated starvation table. Infer from `tasks.state='open'`, `tasks.priority/created_at/lease_id/owner_agent_id`, dispatch ordering, `observability_events.name IN ('dispatcher.routing.task_skipped','worker.routing.task_skipped','worker.routing.no_candidate')`, skip `detail.reason/reason_class/candidate_rank`, `agents.dispatch_hold*`, and `agent_provisioning_requests.reason='dispatch.no_eligible_agent'`. | `src/mac/services.py` dispatch windows, priority aging, availability reasons, and routing skip logs; `tests/test_control_plane.py` priority-aging/starvation coverage; `tests/test_failure_diagnosis.py`; `tests/fault_replay/reviewer_starvation_probe.py`; `tests/test_agent_dispatch_hold.py`, `tests/test_task_no_dispatch.py`. | Build episodes by bucketing repeated skip/no-candidate observations for an open task until a lease appears or the task leaves `open`. Include computed age bonus from `created_at` and the current `MAC_DISPATCH_PRIORITY_AGING_SECONDS` value in reports. |
| Optimizer review metrics | `scientific_policies`, `scientific_experiments`, `scientific_assignments`, `scientific_observations.metrics`, `scientific_decisions`, `scientific_optimizer_events`; supporting `observability_events` `llm.route` rows; task/review/publication rows above. | `src/mac/scientific_optimizer.py`; `src/mac/review_experiments.py`; `docs/scientific-optimizer.md`; `tests/test_scientific_optimizer.py`, `tests/cli/test_cli_optimizer.py`, `tests/cli/test_cli_review_experiment.py`. | Prefer persisted `scientific_observations.metrics` for optimizer analyses. Regenerate with `optimizer.observe_task` or `refresh_experiment` when delayed labels arrive, then analyze decisions by arm/phase and sample eligibility. |

Minimum sample queries:

```sql
-- Evidence manifest types captured for a task.
SELECT json_extract(metadata, '$.verification.evidence_type') AS evidence_type,
       COUNT(*) AS count
FROM evidence
WHERE task_id = :task_id
GROUP BY evidence_type;

-- Review status and linked verdict evidence.
SELECT r.status,
       json_extract(e.metadata, '$.verification.verdict') AS verdict,
       COUNT(*) AS count
FROM reviews r
LEFT JOIN evidence e ON e.id = r.evidence_id
WHERE r.task_id = :task_id
GROUP BY r.status, verdict;

-- Delayed quality labels.
SELECT json_extract(value, '$.kind') AS kind,
       json_extract(value, '$.status') AS status,
       COUNT(*) AS count
FROM tasks, json_each(tasks.metadata, '$.review_outcomes')
WHERE tasks.id = :task_id
GROUP BY kind, status;

-- Optimizer KPI projection.
SELECT arm,
       json_extract(metrics, '$.accepted_success') AS accepted_success,
       json_extract(metrics, '$.review_attempts') AS review_attempts,
       json_extract(metrics, '$.escaped_defect_severity') AS escaped_defect_severity
FROM scientific_observations
WHERE task_id = :task_id;
```

A disposable `ControlPlane.in_memory()` fixture with one executor evidence row,
one approved reviewer verdict, one publication, one confirmed escaped-defect
label, and one optimizer observation produced these counts:

| Query | Result |
| --- | --- |
| task core | `state=completed`, `attempt_count=1`, `review_arm=blind`, first delayed outcome `escaped_defect` |
| task history | `task.created=1`, `task.claimed=1`, `task.evidence_added=2`, `task.review_requested=1`, `task.review_completed=1`, `task.published=1`, `task.review_outcome_recorded=1`, `task.transitioned=4` |
| evidence manifest types | `repo_change=1`, `review_verdict=1` |
| reviews/publications | `approved review=1`, `publication=1` |
| optimizer rows | `scientific_assignments=1`, `scientific_observations=1` |
| optimizer metrics | `accepted_success=1.0`, `review_attempts=1.0`, `escaped_defect_severity=3.0` |

Current gaps to preserve in analysis output:

- Starvation has durable ingredients but no first-class episode table; reports
  must label it as inferred from skip/no-candidate logs plus open-task age.
- Reviewer relationship has explicit model and agent signals, but persona/tenant
  relationship should be reconstructed through current agent/persona rows and
  may be ambiguous for old headless agents.
- Delayed escaped defects require operator or downstream labels; absence of an
  `escaped_defect` outcome is unknown unless a confirmed `clean_window` or
  elapsed optimizer horizon makes the sample quality-valid.
- Cost is missing, not zero, when neither route telemetry nor the model catalog
  can price every observed model route.

## Interpretation limits

Confirmed incremental reviewer findings are direct evidence that the review
stage found something the executor did not resolve before submission. Refuted
findings estimate false-positive cost, while escaped defects estimate false
negatives. This supports an empirical answer to “is the second model adding
value?” but does not by itself establish causality across dissimilar tasks.

For a defensible comparison, pre-register the hypothesis and strata, randomize
comparable tasks, retain assignment propensities, adjudicate findings without
knowing the arm where practical, and keep observing escaped defects after
merge. Compare quality, completion, latency, and cost together. Do not promote
an arm merely because it won a tiny run or because both arms tied at zero;
MAC reports ties as `inconclusive`.
