!!! warning "Historical field note"
    This record preserves ground-truth investigation evidence for a single dream
    finding. It is not a current operating contract; use the numbered book and
    current runbooks for instructions.

# Ground Truth: dream finding `dreamrepair:477446f5c8b8bf1972f2ad31444c956b` (slack failure_pattern)

**Task**: Establish the ground truth behind a low-confidence dream-cycle
`failure_pattern` finding, provider label `slack`, fingerprint
`dreamrepair:477446f5c8b8bf1972f2ad31444c956b`. Retrieve and summarize its
single supporting evidence record, identify what the source task actually
changed, and map the config surfaces that touch Slack/persona runtime and the
`openclaw|none` constraint. Investigation only — no code, skill, tool, config,
or deploy edits.

**Sole evidence record**: `mem_627afb5e4e2c4f09b495eea8984a7b10`
(record_type `deployment_learning:mac`, review_verdict "Convert fleet config to
OpenClaw terminology and constrain Slack/persona runtime to `openclaw|none`"),
authored by `hub-review-workflow`.

**Originating source task**: `task_2a25617199eb43dd8cf95de6c40ef0a1` — "Convert
fleet config to OpenClaw terminology and constrain Slack/persona runtime to
`openclaw|none`" (`evidence_type=repo_change`).

**Prepared by**: fleet worker (investigation node; no production code, test,
skill, config, or deploy edits).

## Verdict: NO LIVE DEFECT — the sole evidence is a *success* record; the "slack" label is incidental

This finding is even weaker than its sibling
(`dreamrepair:cc1dedb0d3036d289aafc1e42b4a22aa`,
`docs/investigation-dreamrepair-cc1dedb0-slack.md`). Both trace to the same
source task, but this fingerprint's lone supporting record,
`mem_627afb5e4e2c4f09b495eea8984a7b10`, is the **terminal** hub-review outcome
`approved_published` — the strongest positive signal the pipeline emits — not a
failure. There is no failing suite, assertion, trace, or reproducer to repair.
The `slack` provider label is derived verbatim from the source task's
title/description wording ("constrain **Slack**/persona runtime"), not from any
Slack transport, chat-gateway runtime, or persona-binding failure.

No files under `src/mac/`, `tests/`, `skills/`, `.mac/`, or `deploy/` were
modified by this investigation.

## What the sole evidence record actually asserts

`mem_627afb5e4e2c4f09b495eea8984a7b10` is the review-stage lesson emitted by the
hub when the source task published to `main`. It is written by
`_record_review_outcome_lesson` (`src/mac/services.py:21555`) with the
`mac.deployment_learning.v1` shape:

- `evidence_type`: `review_verdict`
- `outcome`: `approved_published`
- `signals`: `{"stage": "hub_review"}`
- `error_signature`: **empty string** — the emitter sets `error_signature=""`
  whenever `outcome == "approved_published"`
  (`src/mac/services.py:21578`). There is no stack trace, no failing test id,
  and no reproduction; the `review_verdict`/`detail` carries only the task title
  and the publication target.

Because this record encodes a *published, approved* outcome with an empty error
signature, it cannot localize a defect. A `failure_pattern` keyed on it is a
provenance artifact, not a fault.

## What the source task actually changed (and it landed intact on `main`)

`task_2a25617199eb43dd8cf95de6c40ef0a1` converted the fleet chat-gateway config
to OpenClaw terminology and added a persona/Slack runtime selector allow-list
constrained to `{openclaw, none}`, mirroring the `network.provider` allow-list
pattern. It went `running → needs_review → reviewing → approved → published to
git://main → completed`; the record above is that publication's lesson. The
intended change is present in the tree:

- Persona/Slack runtime selector allow-list — `gateway_impl` must be `openclaw`
  or `none`, or validation fails loudly (`src/mac/fleet_setup.py:208`,
  "gateway_impl must be openclaw or none").
- OpenClaw defaults read from the `openclaw:` block with a backward-compatible,
  read-only `hermes:` fallback (`src/mac/fleet_setup.py:194`).
- Backward-compatible wire format: per-fleet and per-agent output is still
  emitted under the legacy `hermes` key (`src/mac/fleet_setup.py:295`,
  `src/mac/fleet_setup.py:502`) so persisted `fleets.yaml` and the deploy
  payload contract keep loading.

## Config surfaces implicated (Slack/persona runtime + `openclaw|none`)

- `src/mac/fleet_setup.py` — the `gateway_impl` `{openclaw, none}` allow-list and
  `openclaw`/legacy-`hermes` defaults resolution; the primary surface the source
  task edited.
- `src/mac/hermes_config_surface.py` — fleet-scoped OpenClaw configuration
  inspection/apply; retains `defaults.hermes` schema keys for wire-format
  compatibility while accepting the OpenClaw-named block first
  (`src/mac/hermes_config_surface.py:9`).
- `.mac/project.yaml` — project contract that references hub/gateway hosts.
- `src/mac/worker.py` — worker-side runtime consumer of the fleet config.
- `src/mac/_hermes/gateway/platforms/` — the vendored chat-gateway transports
  (including `slack`), which the finding label points at but which nothing in
  the evidence implicates.

## Why the `slack` label is incidental, and why confidence is `low`

The dream classifier's provider matcher includes a bare-word rule
`(r"\bslack\b", "slack")` (`src/mac/dream_cycle_classifier.py:143`). The source
task's title/description contains the word "Slack", so the classifier echoes a
`slack` provider area even though no Slack surface failed. The `low` confidence
(`0.35`) is the classifier's deterministic single-record structural floor for
`support < 2` (`src/mac/dream_cycle_classifier.py:15`,
`src/mac/dream_cycle_classifier.py:87`), i.e. an evidence-volume signal, not a
corroborated defect. No skill, tool, or `repo_area` is co-flagged.

## Recurring pattern or isolated event?

**Isolated.** The finding is backed by exactly one `deployment_learning:mac`
record, and that record is a success (`approved_published`). Any later
re-emissions are the same nap window re-scoring the same lone record, not
independent occurrences.

## Durable, non-Slack takeaway (out of scope here)

The generic issue is an evidence-provenance gap in the dream pipeline: the
`failure_pattern` classifier consumed a `deployment_learning` record for a task
whose terminal outcome is `approved_published` and treated the incidental
task-title wording ("Slack") as a provider signal. A durable improvement belongs
to the classifier/consolidator — do not raise a `failure_pattern` from a record
whose `outcome` is a terminal success, and prefer the latest terminal outcome
per `task_id`. This is recorded as a follow-up consideration only and is
**not** implemented here, per the directive not to change skills, tools, or
config.

## Reproduction (read-only, hub queries)

- `mac task show task_2a25617199eb43dd8cf95de6c40ef0a1 --json` — shows the
  approved/published/completed path and the `git://main` publication.
- `mac admin memory search --task-id task_2a25617199eb43dd8cf95de6c40ef0a1 --json` —
  shows `mem_627afb5e4e2c4f09b495eea8984a7b10`
  (`outcome=approved_published`, `stage=hub_review`) among the task's
  `deployment_learning:mac` records.
