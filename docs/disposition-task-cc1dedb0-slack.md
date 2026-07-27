!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a
    current operating contract; use the numbered book and current runbooks for
    instructions.

# Disposition: low-confidence dream finding `slack` (`dreamrepair:cc1dedb0d3036d289aafc1e42b4a22aa`) — not actionable

**Finding**: a low-confidence dream-repair `failure_pattern`, scope `project`,
project `mac`, provider label `slack`, fingerprint
`dreamrepair:cc1dedb0d3036d289aafc1e42b4a22aa`, confidence `low`
(`overall_confidence_score = 0.35`), backed by a single evidence record.
**Evidence source**: this disposition consumes the evidence-review summary
`docs/investigation-dreamrepair-cc1dedb0-slack.md`, which characterized the sole
supporting record `mem_843510f5c2344eab881036d529b392a5`
(`deployment_learning:mac`) from originating task
`task_2a25617199eb43dd8cf95de6c40ef0a1` ("Convert fleet config to OpenClaw
terminology and constrain Slack/persona runtime to `openclaw|none`").
**Dispositioned by**: fleet worker (disposition only; no production code, test,
skill, config, or deploy edits).

## Disposition: CLOSE — NOT ACTIONABLE (evidence-provenance artifact)

Close the finding as **not actionable** as a defect of the mac chat-gateway,
persona, or Slack runtime surface. The finding is the deterministic output of the
dream/nap consolidation heuristics acting on a single *preliminary*,
executor-authored self-snapshot that was superseded within the same task run. No
concrete, reproducible defect exists for a config, allow-list, terminology, or
code change to fix, so the correct handling is to record this disposition as
investigation evidence and make **no** change to skills, tools, config, or
source.

## Acceptance-criteria evaluation

- **(1) Is the `failure_pattern` real and recurring, or a single low-confidence
  artifact?** A single low-confidence artifact. The record is explicitly
  "Supported by 1 memory record(s)"; the `0.35` score is the classifier's
  deterministic single-record structural floor for `support < 2` (see the `low`
  tier documented at `src/mac/dream_cycle_classifier.py:15` and
  `src/mac/dream_cycle_classifier.py:87`), not a corroborated fault. The later
  `dream:failure_pattern` re-emissions are the same nap window re-scoring the
  same lone record, not independent occurrences.

- **(2) Is the Slack provider association signal or noise, given no
  skills/tools/repo-areas were flagged?** Noise (incidental). The `slack`
  provider label is echoed verbatim from the originating task's title/description
  wording ("constrain **Slack**/persona runtime"), not derived from any Slack
  transport, chat-gateway, or persona-binding failure. With no skill, tool, or
  `repo_area` co-flagged, there is nothing that localizes a Slack-surface defect.

- **(3) Smallest repair vs. follow-up plan.** Neither is warranted as a product
  fix. The change the originating task was about **landed and is intact on
  `main`**: the persona/Slack runtime selector allow-list `{openclaw, none}` is
  present and enforced (`src/mac/fleet_setup.py:208`, "gateway_impl must be
  openclaw or none"), reading the OpenClaw defaults block with a legacy `hermes:`
  read-only fallback (`src/mac/fleet_setup.py:194`). There is no failing suite,
  assertion, trace, or reproducer tied to this finding to repair.

## Why the sole evidence is not a defect

Per the evidence-review, `mem_843510f5c2344eab881036d529b392a5` is a mid-run
executor self-snapshot: `outcome=failure` with `returncode=0`, `tests=fail`,
`pushed=false` — the shape the executor records **before** the contract suite is
finalized and before the guarded push, not the shape of a terminal failure. Its
`error_signature` is the run's own prose change summary (no offending module, no
failing test id, no reproduction). The originating task then went
`needs_review → reviewing → approved → published to git://main → completed`, and
a **second** `deployment_learning:mac` record for the same `task_id`
(`mem_627afb5e4e2c4f09b495eea8984a7b10`, authored by `hub-review-workflow`)
records the authoritative `outcome=approved_published`. The finding rests on the
earlier, superseded snapshot only.

## Evidence gap — what would raise confidence

This disposition is a close, not a permanent verdict. State the gap plainly:
**one** supporting record, confidence `0.35`, a preliminary self-snapshot rather
than a terminal outcome, and a generic `slack` label with **no** named skill,
tool, or `repo_area`. Reopen only if **independent, reproducible** evidence
appears that the Slack/chat-gateway/persona surface actually misbehaves —
concretely, any of:

- **A failing contract test or reproducer** pinning a concrete defect in the
  Slack transport, chat-gateway runtime, or persona binding, reproducible via
  `scripts/run-contract-tests.sh` (e.g. against
  `src/mac/_hermes/gateway/platforms/slack.py` or `src/mac/fleet_setup.py`).
- **A concrete defect location** replacing the incidental `slack` provider label
  — an actual failing module/tool/`repo_area`, not task-title wording.
- **Independent corroboration** raising `evidence_count` to `>= 2` from records
  that are not the same run's preliminary self-snapshot, so confidence can reach
  `medium`/`high` on its own merits.

## Durable, non-Slack takeaway (out of scope here)

The real generic issue is an evidence-provenance gap in the dream pipeline, not a
Slack defect: the classifier keyed on an executor **preliminary**
`deployment_learning` snapshot (`outcome=failure`, `pushed=false`) and did not
reconcile it against the **terminal** `approved_published` record for the same
`task_id` (emitted at `src/mac/services.py:21547`). A durable improvement belongs
to the classifier/consolidator — prefer the latest terminal outcome per
`task_id`, or suppress preliminary `failure` snapshots once a later
`approved_published` record exists for the same task. This is recorded as a
**follow-up consideration only** and is explicitly **not** implemented here, per
the directive not to change skills, tools, or config.

## Verification

Independently re-confirmed in the task-owned worktree with the bootstrapped
`.venv` (all sources read, not modified): the persona/Slack runtime allow-list
and OpenClaw fallback are present and enforced in `src/mac/fleet_setup.py`, the
`0.35` single-record confidence floor and area vocabulary are documented in
`src/mac/dream_cycle_classifier.py`, and the terminal `approved_published`
outcome is emitted by `src/mac/services.py`. No `src/`, `tests/`, `skills/`,
`.mac/`, or `deploy/` file was modified by this disposition; the only artifact is
this field note.
