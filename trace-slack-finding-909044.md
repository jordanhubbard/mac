# Evidence-Chain Trace: Low-Confidence "slack" Dream Finding `dreamrepair:909044938992adfd542851fedc000884`

Investigation-only deliverable for plan node `trace_evidence` of the parent audit
`task_a1c04f4c1c4e4720bb8bfdf693123c73` (title: "Investigate low-confidence dream
finding: slack"). It records ground truth only and changes **no** `src/`,
`tests/`, `skills/`, `deploy/`, tool, or provider code. Output is fleet-generic:
no secrets, host names, personal paths, or operator identities.

The finding under audit is a `failure_pattern`, scope=project, provider label
`slack`, confidence `low` (0.35), 1 evidence record. Per the parent contract the
candidate task to dereference is `task_b3abd9241f3943168ca67c77ef81b494`.

## Summary

Walking the candidate lineage backward through the hub API
(`GET /tasks/<id>`, base `MAC_HUB_URL`, Bearer `MAC_HERMES_GATEWAY_API_KEY`)
resolves a **42-task self-referential chain**. Each generation is a dream-repair
investigation whose sole supporting record is the *outcome memory of the previous
investigation of the same "slack" pattern*. Of the 42 hops, **38 are `failed`,
2 `cancelled`, and 2 `completed`**; 41 carry a `dreamrepair:` fingerprint and
exactly one does not.

The one link without a dream fingerprint is the true origin:

- `task_2a25617199eb43dd8cf95de6c40ef0a1` — **completed**, reviewer-approved
  `repo_change` titled *"Convert fleet config to OpenClaw terminology and
  constrain Slack/persona runtime to openclaw|none"* (created 2026-07-24).

Its **successful** outcome/review memory (`mem_627afb5e4e2c4f09b495eea8984a7b10`,
`record_type deployment_learning:mac`, `review_verdict` = that same title) was
picked up by the dream classifier via a bare `\bslack\b` token match on the task
title and mislabeled as a project `failure_pattern` for provider `slack`. That
task did not fail and describes no Slack-provider defect; it was a
config-terminology refactor that merely *mentions* Slack in its title.

**Original (non-dream-generated) signal:** a single **success** record — the
approved outcome of the OpenClaw-terminology config refactor
`task_2a25617199eb43dd8cf95de6c40ef0a1`. There is **no** genuine Slack failure,
error, stack trace, or failing test anywhere in the chain. Every later
`failure_pattern` fingerprint (including `dreamrepair:909044938992adfd542851fedc000884`
and its candidate's `dreamrepair:42931354a0c3d41ddf3b6365e5e254fc`) is a
regeneration seeded by a prior investigation's own outcome memory.

## Ordered evidence-chain table

Ordered newest-first, exactly the trace direction requested: hop 0 is the audited
candidate; each subsequent row is the predecessor referenced by that hop's
candidate summary (`task=<prior_id>`) or originating-task reference. Hop 41 is the
non-dream origin.

