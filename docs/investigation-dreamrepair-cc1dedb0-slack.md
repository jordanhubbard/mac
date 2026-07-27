!!! warning "Historical field note"
    This record preserves ground-truth investigation evidence for a single dream
    finding. It is not a current operating contract; use the numbered book and
    current runbooks for instructions.

# Ground Truth: dream finding `dreamrepair:cc1dedb0d3036d289aafc1e42b4a22aa` (slack failure_pattern)

**Task**: Characterize the sole evidence backing a low-confidence dream-repair
finding of kind `failure_pattern`, scope `project`, project `mac`, provider
label `slack`, fingerprint `dreamrepair:cc1dedb0d3036d289aafc1e42b4a22aa`,
confidence `low` (score `0.35`). Determine the concrete failure mode, the
affected code/config surface (if any), and whether the finding is a recurring
pattern or an isolated artifact.

**Sole evidence record**: `mem_843510f5c2344eab881036d529b392a5`
(record_type `deployment_learning:mac`, schema `mac.deployment_learning.v1`),
authored by `mac-task-executor`.

**Originating task**: `task_2a25617199eb43dd8cf95de6c40ef0a1` — "Convert fleet
config to OpenClaw terminology and constrain Slack/persona runtime to
openclaw|none" (`evidence_type=repo_change`).

**Prepared by**: fleet worker (investigation node; no production code, test,
skill, config, or deploy edits).

## Verdict: NO LIVE DEFECT — stale preliminary self-evidence; the "slack" label is incidental

The finding rests on a single mid-run, executor-authored self-snapshot that was
superseded within the same task. The originating task did not fail: it was
reviewed, approved, and published to `main`, and a second deployment-learning
record for the same task records the authoritative outcome
`approved_published`. The `slack` provider label is derived from the task
*title/description* wording ("Slack/persona runtime"), not from any Slack
runtime, chat-gateway, or persona execution failure. This is an isolated,
non-recurring evidence-provenance artifact, not an actionable defect.

No files under `src/mac/`, `tests/`, `skills/`, `.mac/`, or `deploy/` were
modified by this investigation.

## What the sole evidence record actually says

`mem_843510f5c2344eab881036d529b392a5` (created `2026-07-26T09:18:19Z` by
`mac-task-executor`) carries this `mac.deployment_learning.v1` payload:

- `outcome`: `failure`
- `evidence_type`: `repo_change`
- `signals`: `tests=fail`, `returncode=0`, `pushed=false`, `files_changed=4`,
  `checks_pass=null`
- `error_signature`: **not a stack trace or failing assertion** — it is a
  truncated prose recap of the change the worker performed ("Converted the fleet
  chat-gateway config and inspection surfaces to OpenClaw terminology, added a
  persona/Slack runtime selector allow-list (openclaw|none) mirroring
  network.provider, and dropped 'herm…").

Two facts make this a *preliminary* snapshot rather than a terminal failure:

1. `returncode=0` with `tests=fail` and `pushed=false` is the shape the executor
   records **mid-run**, before the contract suite has been finalized and before
   the guarded push — not the shape of a task that terminated in failure.
2. The `error_signature` names no offending module, no failing test id, and no
   reproduction. It is the run's own change summary, so it cannot be used to
   locate a defect.

## The originating task succeeded — the failure was superseded

The task ledger for `task_2a25617199eb43dd8cf95de6c40ef0a1` shows a clean
success path, all within a single lease:

- `running` → `needs_review` (worker submitted `worker-result.json`, 8
  artifacts) at `09:18:57Z`.
- `needs_review` → `reviewing`; an independent reviewer was assigned.
- Review **approved** via signed verdict evidence at `09:23:25Z`.
- Canonical branch integration verified; **published** to `git://main`
  (`pub_a802c30011fd4705a618afa428e2277f`) and transitioned to **completed** at
  `13:07:10Z`.

Crucially, a **second** `deployment_learning:mac` record for the same task,
`mem_627afb5e4e2c4f09b495eea8984a7b10` (created `2026-07-26T13:07:10Z` by
`hub-review-workflow`), records `outcome=approved_published`
(`signals.stage=hub_review`). The dream classifier consumed only the earlier
`failure` snapshot and did not weigh the later authoritative
`approved_published` record.

## The intended change landed and is intact on `main`

The OpenClaw conversion and the constrained runtime selector this task was about
are present in the tree:

- `src/mac/fleet_setup.py` reads the `openclaw:` block with a legacy `hermes:`
  read-only fallback and validates the gateway impl against the allow-list —
  "gateway_impl must be openclaw or none" (`src/mac/fleet_setup.py:208`), the
  allow-list check at `src/mac/fleet_setup.py:208`, and OpenClaw defaults at
  `src/mac/fleet_setup.py:194`.
- `src/mac/hermes_config_surface.py`, `src/mac/worker.py`, and
  `.mac/project.yaml` are all present.

There is no failing suite, assertion, trace, or reproducer tied to this finding
to repair, and no remaining code/config remediation for the mac chat-gateway /
persona / Slack runtime surface.

## Is the "slack" provider label causal or incidental?

**Incidental.** The provider/area label attached to the finding comes from the
task title and description text — "constrain **Slack**/persona runtime" — which
the nap/dream summarizer echoes verbatim into the finding's `observations` and
`summary`. Nothing in the evidence points to a Slack transport, chat-gateway
runtime, or persona-binding failure. The word "Slack" appears only because the
task was *about renaming and constraining* that config surface, not because that
surface failed.

## Recurring pattern or isolated event?

**Isolated.** The dream `failure_pattern` record is explicitly
"Supported by 1 memory record(s)" — a single `deployment_learning:mac` evidence
item (`record_type_counts: {deployment_learning:mac: 1}`). The `0.35`
confidence is the classifier's deterministic single-record structural floor
(an evidence-volume signal), not a corroborated defect. Multiple later
`dream:failure_pattern` records exist for this task, but they are re-emissions
of the same nap window over the same single evidence record, not independent
occurrences.

## Evidence-provenance gap (the real, generic issue)

The finding is a false positive produced by a provenance gap, not a product
defect: the dream/nap failure-pattern classifier keyed on an executor
**preliminary** `deployment_learning` self-snapshot (`outcome=failure`,
`tests=fail`, `pushed=false`) and did not reconcile it against the **terminal**
record for the same `task_id` (`outcome=approved_published`). A durable fix
belongs to the classifier/consolidator (prefer the latest terminal outcome per
`task_id`, or suppress preliminary `failure` snapshots once a later
`approved_published` record exists), and is intentionally **out of scope** for
this investigation, which was directed not to change any skills, tools, or
config.

## Reproduction (read-only, hub queries)

- `mac task show task_2a25617199eb43dd8cf95de6c40ef0a1 --json` — shows the
  approved/published/completed path and the `git://main` publication.
- `mac memory search --task-id task_2a25617199eb43dd8cf95de6c40ef0a1 --json` —
  shows both `deployment_learning:mac` records (preliminary `failure` and
  terminal `approved_published`) plus the single-evidence
  `dream:failure_pattern` records that cite only the former.
