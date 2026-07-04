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
different model. Experimentation cannot weaken signature, test, CodeGraph,
publication, or reviewer-eligibility gates.

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

```bash
mac task create "Add parser boundary tests" --project=nanolang --no-dispatch

mac review experiment assign TASK_ID review-protocol-2026-07 \
  --arms standard=1,blind=1 \
  --blind-arm blind \
  --policy-version=v1 \
  --hypothesis-file=hypothesis.txt \
  --stratum=small-maintenance

mac task release TASK_ID
mac dispatch tick --limit 10
```

For a fixed arm, use `--arm standard` or `--arm blind --blind`. The explicit
assignment propensity defaults to 1; set `--probability` only when the arm was
selected by an external randomized policy whose propensity is known.

Inspect one derived lifecycle:

```bash
mac review experiment observe TASK_ID
```

Review findings start unresolved. After reproduction or downstream
observation, append a durable label without altering the signed evidence:

```bash
mac review experiment outcome TASK_ID finding_validation confirmed \
  --finding-id=FINDING_ID \
  --severity-weight=2 \
  --source=operator-reproduction \
  --detail-file=validation.json

mac review experiment outcome TASK_ID clean_window confirmed \
  --severity-weight=0 \
  --source=post-merge-ci \
  --detail-file=window.json

mac review experiment outcome TASK_ID escaped_defect confirmed \
  --severity-weight=4 \
  --source=incident \
  --detail-file=incident.json
```

The accepted outcome states are `pending`, `confirmed`, and `refuted`.
`finding_validation`, `clean_window`, and `escaped_defect` are the outcome
kinds counted by the policy gate; other kinds remain visible but do not satisfy
the minimum validation threshold.

Derive an experiment report:

```bash
mac review experiment report review-protocol-2026-07 --project=nanolang
```

The default gate requires five terminal, reviewed tasks and three counted
outcomes in every arm. Lower thresholds are useful for plumbing validation,
but are not evidence of scientific superiority:

```bash
mac review experiment report review-protocol-2026-07 \
  --project=nanolang \
  --min-tasks-per-arm=2 \
  --min-validated-outcomes-per-arm=1
```

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

