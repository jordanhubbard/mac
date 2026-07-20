# Managed-versus-legacy execution telemetry

MAC records the execution route before task outcomes are known so comparisons
between the legacy asynchronous worker path and the managed synchronized path
do not have to infer treatment from mutable task state or prunable logs.

## Durable records

`execution_cohort_assignments` contains one immutable assignment per task or
work package. A task row is the randomization unit; a package row records the
managed execution authority. `root_task_id` is lineage only and never rewrites
the task's assignment.

- `eligibility` is `eligible`, `ineligible`, or `unknown`;
- `treatment_route` is `legacy_async`, `managed_synchronized`, or the
  historical-only `unknown_managed_mode`;
- `rollout_revision` identifies the experiment configuration revision;
- `cohort_key`, `reason`, and secret-free `detail` retain the policy decision
  and its blockers; and
- `assigned_at` is committed before either route performs readiness checks,
  remote base attestation, or materialization. A treatment-assigned request
  therefore remains observable even when its selected route cannot create a
  task or package.

The primary cohort contains requests that are all of the following at task
creation: explicitly atomic (`no_decompose`), dependency-free, outside the
workflow runtime, backed by a strong registered-repository execution contract,
repository-mutating rather than report-producing, and submitted with the
default `publication_lane_policy=auto` after the managed rollout is ready.
Operator-forced `managed` or trusted `legacy` policies remain supported but are
marked `ineligible` and excluded from the primary analysis.

Eligible requests are assigned concurrently by
`hmac_sha256_bucket_v1(task_id, experiment_revision)`. The HMAC key is derived
from `MAC_EXECUTION_COHORT_SEED` (or, if absent, `MAC_SECRET_KEY`) and is never
persisted. Only a bucket, short allocation fingerprint, algorithm version,
revision, and percentage are stored. Configure:

```text
MAC_EXECUTION_COHORT_REVISION=1
MAC_EXECUTION_COHORT_TREATMENT_PERCENT=50
MAC_EXECUTION_COHORT_SEED=<stable secret, at least 32 characters>
```

Changing the percentage or seed requires incrementing the revision. The hub
atomically creates an immutable `execution_cohort_configurations` row before
the first assignment and fails closed if replicas present a different
percentage, algorithm, or derived-key fingerprint for that revision. The
fingerprint is not key material. Neither an operator nor a worker selects the
primary cohort route.

Fleet deployment uses hub-only controls rather than placing runtime settings
on every node:

```text
MAC_DEPLOY_EXECUTION_COHORT_REVISION=1
MAC_DEPLOY_EXECUTION_COHORT_TREATMENT_PERCENT=50
MAC_DEPLOY_EXECUTION_COHORT_SEED=<stable secret, at least 32 characters>
```

The deployer maps these to the three runtime names above only in the hub's
mode-0600 environment file. Revision and percentage use ordinary deployment
configuration; the seed crosses SSH through the one-use secret stdin file and
never appears in the remote command or process arguments. Spokes receive none
of the deploy or runtime cohort values, and a former hub has stale values
removed when redeployed as a spoke. If the deploy seed is omitted, the hub
retains an existing runtime seed or falls back to `MAC_SECRET_KEY`; an explicit
stable seed is recommended for the pilot so hub-secret rotation cannot change
assignment identity. Never copy the seed into a task, ledger record, image,
fleet registry, command line, or operator log.

`work_package_station_attempts` is append-only. It records admission,
integration, certification, landing, and finalization observations with the
exact package generation, operation, queue/start/completion timestamps,
durations, attempted flag, terminal status, reason code, and failure class.
Controller reports use `(pipeline_run_id, outcome_index)` as an idempotency
key, so observer retries do not double-count an attempt.

`work_package_controller_outcomes` is the loss-preserving append-only ledger
for every controller report delivered to the observer. The raw report is
committed before station normalization, so a stale package link or an unknown
future operation cannot erase the diagnostic input. It records package-less
inventory failures, terminal `complete` observations, generic certification
states, and future/unmapped operation names. Mapped package outcomes are also
normalized into station attempts; unknown operations go to the `controller`
station with `unmapped_controller_operation` instead of disappearing. This is
not a transactional write-ahead log for station actions: a controller crash
after a station commits but before its report reaches the observer can omit a
secondary attempt row. The primary publication endpoint is instead derived
from the authoritative task/publication and package/finalization/rejection
receipts, which survive that reporting window.

`work_package_telemetry_health` is a direct, non-recursive health record. The
pipeline observer attempts ordinary logging and measurement independently. A
measurement failure increments the durable counter with only the operation,
error type, and a SHA-256 fingerprint; a later successful measurement clears
the alert state by advancing `last_success_at` without erasing the count.

`work_package_finalization_outcomes` links a later `revert` or `incident` to the
exact append-only finalization receipt. Recording a link requires admin scope;
the linked outcome does not rewrite the original finalization.

These record families, plus `work_package_history`, are projected into the
unified `/events` surface. The `work_package` subject type can be filtered by
package ID. Package-less controller events use the `service` subject.

All new measurement timestamps are normalized to fixed-precision UTC `Z`
form before storage or filtering. If replica clock skew would create a negative
queue or execution duration, the stored duration is clamped to zero and the
original value plus clamp reason is retained in `detail.clock_clamps`.

## Export

The bounded read surface is:

```text
GET /work-package-telemetry
GET /work-package-telemetry/comparable-atomic-outcomes
GET /work-packages/{package_id}/telemetry
```

The comparable atomic projection uses a left join from assignment to task.
`task_materialized=false` is retained rather than dropped, so route-specific
admission failures cannot turn the treatment arm into a success-only sample.
Prospective assignments for this endpoint use
`mac.execution_cohort.prospective.v3`; earlier prospective schemas are kept
immutable but are not silently pooled into the v3 primary analysis.
The response schema is
`mac.execution_cohort.comparable_atomic_outcome.v2`. Its primary fields are
`canonical_publication_outcome`, `canonical_publication_success`,
`canonical_publication_terminal_at`, and
`assignment_to_canonical_publication_terminal_duration_ms`. The shared origin
is always `assigned_at`. Route-internal task state and timing are nested under
`secondary_task_metrics` and must not be substituted for the primary endpoint.

The two arms reach that endpoint as follows:

- legacy success requires an append-only `publications` row. A Git-main target
  additionally requires the controller's `mac.canonical_integration.v1` proof
  that the reviewed head is remotely present in the canonical tip;
- managed success requires the atomic
  `work_package_publication_finalizations` receipt, not completion of an
  upstream mutation or controller task;
- a legacy terminal task without the required publication proof is a terminal
  failure;
- a managed `failed` or `cancelled` package is a terminal failure. A package
  marked completed without its required finalization is also fail-closed as a
  terminal failure;
- an exact failed certification, rejected product batch, quarantined WIP, and
  controller rejection receipt is a terminal negative-canary failure even
  though the Andon projection leaves the package `paused`; and
- a candidate rejection is terminal when its immutable package-history receipt
  says `retry_staged=false` and `remaining_rework_cycles=0`. Other pauses and
  replanning states remain recoverable and therefore censored.

For managed work, the first terminal publication or final-rejection receipt
wins. This prevents a later operator-created replan from rewriting the outcome
of the originally assigned execution.

`/work-package-telemetry` accepts `package_id`, `treatment_route`,
`eligibility`, `station`, `since`, and `limit` filters. The response returns
cohort assignments, raw controller outcomes, station attempts, finalization
outcomes, measurement health, the comparable atomic projection, and explicit
methodological limitations. Route and eligibility filters apply consistently
to finalization outcomes through the package cohort assignment. For example:

```console
curl -H "Authorization: Bearer $MAC_API_TOKEN" \
  'https://hub.example/work-package-telemetry?treatment_route=managed_synchronized&eligibility=eligible&since=2026-07-17T00:00:00Z'
```

A later incident or revert is attached with:

```text
POST /work-package-finalizations/{finalization_id}/outcomes
{
  "outcome_type": "incident",
  "external_id": "incident-123",
  "observed_at": "2026-07-20T12:00:00Z",
  "actor": "operator",
  "detail": {"severity": "high"}
}
```

## Historical control cohort

The historical backfill runs once under the append-only
`execution_cohort_historical_backfill_v2` migration receipt. Existing packages
are `managed_synchronized` only when an immutable publication-finalization
receipt proves the complete synchronized pipeline. Package linkage without
that receipt is `unknown_managed_mode`; it proves management, not receipt of
the current treatment. Existing unlinked tasks are `legacy_async`. Historical
experimental eligibility is always `unknown`, because those tasks have no
prospective HMAC randomization record. When the control-plane-owned
`managed_fast_lane.activation=legacy_compatibility` projection survives, its
atomic-shape evidence is retained separately in `detail.shape_eligibility_source`;
it is not promoted into the later experiment's eligibility contract. The
system does not reconstruct historical eligibility from today's worker
inventory, certification configuration, arbitrary task fields, or rollout
state.

Consequently, historical `unknown` rows are a descriptive control cohort and
must not be pooled with prospectively assigned eligible cohorts. A surviving
shape projection can support stratified descriptive analysis, but it still
lacks prospective station timing and random assignment. Historical station
durations are not synthesized from mutable timestamps.

## Evaluation rules

- The predeclared primary estimand is the intention-to-treat effect of managed
  assignment on canonical-publication success and
  assignment-to-canonical-publication latency among concurrent eligible atomic
  auto-policy requests.
- Use `GET /work-package-telemetry/comparable-atomic-outcomes` as the canonical
  primary projection. It uses the immutable assignment clock as the shared
  origin and semantically equivalent authoritative publication boundaries in
  both arms. It includes nonmaterialized and nonterminal assigned work rather
  than silently dropping it.
- Compare only `eligible` assignments carrying
  `primary_analysis_eligible=true` and the versioned HMAC randomization record.
  Keep `ineligible`, `unknown`, and historical cohorts separate.
- Retain experiment revision, percentage, allocation fingerprint, and
  assignment time in every analysis.
- Treat final certification rejection and exhausted rework as terminal
  failures. Treat recoverable `held`, `busy`, `paused`, and `replanning`
  observations as censored until the predeclared window closes, not as missing
  rows.
- Predeclare an observation window before comparing terminal rates. Retain
  `task_materialized=false` through that window; do not convert it into a
  complete-case exclusion.
- Measure queue and execution duration separately.
- Join later incidents/reverts through finalization outcome links instead of
  matching branch names or commit timestamps heuristically.
- Route-internal task completion, station timings, and materialization are
  secondary process metrics. In particular, managed mutation-task completion
  happens before integration, certification, landing, and finalization and is
  not product success.
- Linked incidents/reverts are secondary post-publication quality outcomes.
  They are not a cross-route quality estimand until legacy execution gains an
  equivalent immutable product receipt/outcome link.
- Do not infer success from absence of an error log. Use terminal status and
  the append-only receipt/event chain.
