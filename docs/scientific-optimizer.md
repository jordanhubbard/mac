# Autonomous scientific optimizer

MAC's scientific optimizer turns execution-policy choices into falsifiable,
replayable experiments. It continuously derives task KPIs from the task ledger,
router observations, reviews, publications, and delayed outcome labels; proposes
one bounded treatment at a time per project; randomizes eligible repository
tasks; and promotes a treatment only when it is statistically superior and all
quality endpoints are non-inferior.

It optimizes policy, not safety. The only mutable task fields are `model`,
`review_model`, `model_strength`, `review_model_strength`, `max_iterations`,
`review_max_iterations`, `plan_first`, and `review_mode`. Sandbox policy,
required tests, review eligibility, signatures, publication,
authorization, and deployment gates are not policy parameters and are rejected
by validation.

## Durable control loop

The hub persists five replayable records:

1. A versioned policy contains only allowlisted parameters. One policy is active
   per project; every promotion or rollback is an audited event.
2. A pre-registered experiment names its hypothesis, control and treatment,
   primary KPI, expected direction, minimum effect, sample budget, exploration
   rate, outcome horizon, and guardrails.
3. A task assignment records the arm, exact policy, phase, assignment propensity,
   and task stratum before execution. Deterministic hashing makes assignment
   reproducible without making it predictable from the policy itself.
4. An observation projects terminal ledger evidence into canonical KPIs. It can
   be regenerated after delayed labels arrive.
5. A decision stores sample counts, estimates, confidence bounds, every
   guardrail result, and the action taken.

The scheduler uses a database lease, so one replica owns a cycle even when the
API is deployed with multiple Postgres-backed hub replicas. A project also has a
single durable experiment slot. An experiment begins with balanced 50/50 A/B
assignment inside its exploration sample. A winner enters monitoring with 90%
treatment and 10% control traffic. A monitoring guardrail regression rolls the
project back to the registered control policy.

## KPIs and inference

The canonical projection includes accepted and delayed-quality success,
executor/reviewer attempts, rework cycles, lead time, model latency, input and
output tokens, estimated USD cost, escaped-defect severity, and publication
count. Cost is taken from explicit route telemetry when available or derived
from the native models.dev catalog. Unpriced calls are missing cost data; they
are never counted as zero-cost samples.

Only terminal tasks whose quality outcome is validated enter an analysis. A
completed task becomes delayed-quality-valid after its configured observation
horizon or an explicit `clean_window`/`escaped_defect` outcome. Failed and
cancelled tasks are immediately quality-valid failures. Record stronger delayed
labels with `mac admin review experiment outcome`.

MAC uses a deterministic two-sample bootstrap for treatment-minus-control
means. Because the scheduler can inspect an experiment after each sample, the
confidence level is Bonferroni-corrected over every planned interim look and
every tested endpoint. Accepted success, delayed-quality success, and escaped
defect severity are mandatory guardrails; a caller can tighten them but cannot
remove them. Small or inconclusive runs continue until their sample budget is
exhausted and are then rejected, never promoted on a tie.

## Enable and inspect

The scheduler is opt-in at deployment time. Leave it disabled until ordinary
task-flow latency is healthy:

```console
MAC_SCIENTIFIC_OPTIMIZER_ENABLED=0
MAC_SCIENTIFIC_OPTIMIZER_INTERVAL_SECONDS=300
MAC_SCIENTIFIC_OPTIMIZER_INITIAL_DELAY_SECONDS=60
MAC_SCIENTIFIC_OPTIMIZER_AUTO_PROPOSE=1
MAC_SCIENTIFIC_OPTIMIZER_AUTO_PROMOTE=1
MAC_SCIENTIFIC_OPTIMIZER_AUTO_IMPROVE=1
```

Inspect or run one cycle:

```console
mac admin optimizer status
mac admin optimizer tick
```

After a manual tick and ordinary `task ready`/`task stats` calls remain within
the deployment's latency budget, set `MAC_SCIENTIFIC_OPTIMIZER_ENABLED=1` to
run it periodically. Baseline analysis is bounded, indexed, and cached, but it
is still background work.

Automatic hypothesis generation currently makes conservative, one-variable
changes: lower a named strength rung when cost is measurable, reduce an
explicitly oversized reviewer turn budget, or enable plan-first execution when
baseline rework is high. It waits for at least
`MAC_SCIENTIFIC_OPTIMIZER_MIN_BASELINE_TASKS` terminal tasks and never creates a
second active experiment for the same project.

When measured rework remains high after the safe parameter treatments are
exhausted, `AUTO_IMPROVE` creates a normal, dispatchable repository task with
the baseline task IDs and KPI means. That task must identify a causal mechanism,
add missing instrumentation if needed, and pre-register a bounded treatment.
It runs through the ordinary executor, test, independent review, and
publication path; optimizer-origin work is excluded from its own baseline. An
open-task check plus `MAC_SCIENTIFIC_OPTIMIZER_IMPROVEMENT_COOLDOWN_SECONDS`
prevents repeated task generation.

## Manual pre-registration

Policies and hypotheses can be registered without bypassing the evidence gate:

```console
mac admin optimizer policy create baseline nanolang --parameters-file=baseline.json
mac admin optimizer policy promote POLICY_ID --reason="registered baseline"
mac admin optimizer policy create plan-first nanolang --parameters='{"plan_first":true}'

mac admin optimizer experiment create reduce-rework nanolang CONTROL_ID TREATMENT_ID \
  --hypothesis-file=hypothesis.txt \
  --primary-metric=cycles_to_accept \
  --min-effect=0.25 \
  --min-samples-per-arm=8 \
  --max-samples-per-arm=40 \
  --exploration-fraction=0.2
mac admin optimizer experiment start EXPERIMENT_ID
```

Use `mac admin optimizer experiment analyze EXPERIMENT_ID` to refresh all observations.
Use `mac admin optimizer experiment evidence EXPERIMENT_ID` to export the durable
assignments, KPI projections, statistical decisions, and audit events.
When `--no-auto-promote` was registered, `mac optimizer experiment promote
EXPERIMENT_ID` promotes only an already evidence-backed candidate; it is not a
force override. `pause` stops assignment, and `policy rollback` restores a prior
policy with an audit reason.

The same surface is available under `/optimizer`: policies and experiments have
create/list/get/action routes, with explicit observe/analyze/tick endpoints.
Reads require `read`; every mutation requires `admin`.

## Interpretation limits

Randomization supports causal comparisons only for tasks eligible for the same
experiment. Repository, size, quality-contract, and time-period shifts can
still limit external validity. Delayed defects must be labeled; no telemetry
system can infer incidents it never receives. Model catalog prices can also lag
provider billing. Keep policy versions, outcome horizons, and task strata in
reports when comparing results across projects or calendar periods.
