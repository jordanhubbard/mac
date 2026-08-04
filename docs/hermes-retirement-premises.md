# Testing the premises for retiring the vendored Hermes tree

Status: findings, 2026-08-04. Written against `task_d9692288`. Nothing is
implemented or reverted here; this records what the evidence says about two
claims the retirement rests on.

## The two premises

The migration from the vendored Hermes runtime to OpenClaw was justified by:

1. **OpenClaw runs out of the box with no patches**, removing the maintenance
   burden of a patched vendor tree.
2. **Hermes is no longer required because MAC superseded the Hermes learning
   capability.**

Both were checked against the live fleet and the repository on 2026-08-04.
Neither holds as stated.

## Premise 2 — the learning capability

**Not supported. MAC's learning loop runs on schedule and produces no durable
output.**

Sampling 5,000 observability events from the hub:

```text
agent.nap_completed events:        10
  with summary_evidence_id:         0
  with summary_evidence_id = null: 10
window: 2026-08-04T00:55:26 -> 2026-08-04T01:32:40
```

The loop is not dormant — `nap.tick.run` shows `agent_natasha`, `agent_rocky`,
`agent_jordanh-worker5` and `agent_operator` all with `"napped": true,
"skipped": false`. Every run then completes carrying no summary evidence.

The memory store agrees. All 31 entries in `project=mac` are hand-authored
operator or agent notes — `plan-wave-cli-blocker`, `fleet-churn-root-causes`,
`e2e-audit-2026-07-14-defect-taxonomy`. Across the nine days from 2026-07-24 to
2026-08-02, continuous fleet operation, the loop wrote nothing; the five August
entries were written by hand during the 2026-08-02/03 session.

Corroborating from the origin-yield table in
`docs/assessment-2026-08-02.md`: the learning-adjacent generators are the worst
performers measured — `curiosity_adjudication` 0/11, `backlog_grooming` 0/5,
`dream_low_confidence_repair` 4/1396 (0.3%, since deleted).

So MAC has the architecture — `nap_ticker`, `nap_consolidator`,
`curiosity_reviewer`, `dreaming`, `fleet_learning`, `worker_reflect`. What it
demonstrably has in production is a **durable operator-notes store**, which is
genuinely valuable and is what carried knowledge across sessions. That is not
the same claim as superseding a learning capability.

Tracked separately as the null-nap-output defect.

## Premise 1 — the patch burden

**Not supported. OpenClaw is already patched, and the Hermes vendoring is
engineered rather than ad-hoc.**

| runtime | local patches | notes |
|---|---|---|
| Hermes | 12 patches, 3,632 lines, 7 overlay files | reproduces byte-for-byte from pristine upstream |
| OpenClaw | 1 patch + a filed upstream issue | `patch-stuck-session-recovery.py`, openclaw#105586 |

"No patches" is already false. OpenClaw needed
`deploy/openclaw/patches/patch-stuck-session-recovery.py` for a wedge where the
stuck-session watchdog detects a leaked run marker but recovery declines to
reclaim the lane forever — the agent adds reactions but never replies until the
gateway is restarted. It is filed upstream as
`github.com/openclaw/openclaw/issues/105586` and the local patch is to be
dropped once a release ships the fix.

The 12 Hermes patches are also less burdensome than the count suggests.
From `deploy/hermes/LOCAL_PATCHES.md`:

* `zz-public-api-docstrings.patch` is **documentation-only** — no behaviour or
  signature changes.
* `fts5-orphan-schema-recovery.patch` and
  `remove-duplicate-top-level-skills.patch` are **upstream bug fixes** (FTS5
  shadow-table corruption self-heal; ambiguous skill-name shadowing) and are
  candidates for upstreaming rather than permanent local delta.
* The remainder are genuine MAC integration: provider decision, runtime-context
  prompt, sandbox PATH precedence, honest one-shot exit status, multi-Slack.

Crucially the pipeline is reproducible, not a pile of hand edits:
`scripts/vendor-hermes-snapshot.sh` rebuilds the tree from pristine upstream at
a pinned commit through patches, removals and overlay, **validated to reproduce
the committed tree byte-for-byte**, with `tests/test_hermes_vendor_integrity.py`
pinning a content digest so any drift fails loudly.

## What the retirement costs

Independent of the premises, deleting `src/mac/_hermes` removes:

* **25 skill families** — creative, data-science, devops, email, github, media,
  productivity, red-teaming and others. What MAC ships for OpenClaw in this
  repository is one plugin, `mac-continuity`. That is not a like-for-like
  comparison, since OpenClaw brings capabilities that do not live here, but the
  repo-side provision is 25 against 1 and the difference has not been measured.
* **MAC's only NeMo Relay tool/LLM instrumentation.** `relay_tool_context` and
  `relay_llm_context` are wired in exactly two places, both inside the vendor:
  `_hermes/agent/tool_executor.py` and `_hermes/agent/chat_completion_helpers.py`.
  Phase 1 (`create_agent_scope`) is on live paths and survives; Phase 2 does
  not. No test catches this — the suite exercises the seam, not the live call
  path — and Relay is default-ON for every deployed agent
  (`deploy_env.py:1161`), so the telemetry would keep looking enabled. Recorded
  as an ordering dependency on `task_d9692288`.

## What this does NOT conclude

It does not conclude "retreat to Hermes".

The migration is substantially done: OpenClaw is the live gateway, the Hermes
runtime is inactive (`gateway_ownership.services = {hermes: inactive,
nemoclaw: inactive, openclaw: active}`), and only `com.mac.openclaw-gateway`
runs. Reversing has its own cost, and this analysis has **not** measured
OpenClaw's actual capability surface against those 25 skill families — that is
the missing comparison and the thing most likely to change the answer.

What it does conclude is narrower and firmer: **the two stated reasons for the
migration do not currently hold**, so the decision should be re-made on
whatever the real reasons are, rather than on these.

## Suggested next step

Measure the capability surface, not the premises. Enumerate what the 25 vendored
skill families actually provide, which of those the fleet has used in the last
90 days, and which OpenClaw covers today. A skill family nobody has invoked is
not a cost of retirement.

## References

* `deploy/hermes/LOCAL_PATCHES.md` — the patch set and its rationale
* `deploy/openclaw/patches/UPSTREAM-ISSUE-stuck-session-recovery.md`
* `docs/assessment-2026-08-02.md` section 4 — origin yield
* `docs/openshell-nemo-relay-integration.md` — Relay phases
* `task_d9692288`, and the null-nap-output defect filed alongside this note
