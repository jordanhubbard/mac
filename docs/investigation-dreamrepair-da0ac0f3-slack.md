!!! warning "Historical field note"
    This record preserves ground-truth investigation evidence for a single dream
    finding. It is not a current operating contract; use the numbered book and
    current runbooks for instructions.

# Ground Truth: dream finding `dreamrepair:da0ac0f3cab187290c91e5b26a6c5b9f` (slack failure_pattern)

**Task**: `task_ab7b0013115c4a0689287539623c3d55` — "Trace and classify the slack
dream-repair evidence chain". Read-only, investigation-only audit for parent
`task_09cc05cf11414d56aa5aec189218dad4` ("Investigate low-confidence dream
finding: slack"), plan node `trace_evidence`. This document records ground truth
only. It changes **no** `src/`, `tests/`, `skills/`, or `deploy/` file and makes
no skill, tool, or provider repair.

**Prepared by**: fleet worker (investigation node; no production code, test,
skill, config, or deploy edits).

## Verdict

**NOT ACTIONABLE as a slack repair — the finding is a self-referential
dream-repair loop artifact, not a genuine Slack provider/integration failure.**
The sole supporting evidence record is the *closure/outcome memory of a prior
identical "Investigate low-confidence dream finding: slack" task*, not an
observation of a broken Slack integration. Independent inspection of the repo's
real Slack surface shows it is healthy (11/11 Slack tests pass), so no concrete,
reproducible Slack defect exists for a code or skill change to fix. Correct
disposition for the parent audit: close as dismiss / not actionable, and pursue
the upstream pipeline fix (break the self-reference), not a Slack change.

## The Finding's Claim

- fingerprint: `dreamrepair:da0ac0f3cab187290c91e5b26a6c5b9f`
- kind: `failure_pattern`; scope: `project`; project: `mac`
- confidence: `low` (`overall_confidence_score = 0.35`); `evidence_count = 1`
- affected providers: `["slack"]` — a bare provider label, not a diagnosed fault
- affected skills / tools / repo_areas: none
- signal: `\bslack\b` — a plain word-boundary token match
- sole evidence record: `mem_8ae49db9e694426ca469e5ddb0a15499`

Every discriminating field is empty or a generic label. The only "signal" is the
English word "slack" matched by regex `\bslack\b`, matched against text that
embeds a *prior task's own title* ("Investigate low-confidence dream finding:
slack"). A `failure_pattern` with support = 1 and a bare-token signal is exactly
the shape the source heuristics score lowest; `0.35` reflects support < 2, not a
diagnosed defect.

## Candidate / Evidence Chain (sibling tasks)

The task supplied a candidate/evidence lineage. Each link is another
"Investigate low-confidence dream finding: slack" generation whose outcome memory
seeds the next candidate. The chain is self-referential: each generation's
`deployment_learning:mac` outcome memory (whose `task_title` still contains the
word "slack") is consumed as the "evidence" for the next dream candidate.

| Task (generation)                              | Title (as recorded)                                 | State / role                 | Fingerprint / feeding record type                          |
|------------------------------------------------|-----------------------------------------------------|------------------------------|------------------------------------------------------------|
| `task_09cc05cf11414d56aa5aec189218dad4`        | Investigate low-confidence dream finding: slack     | parent audit (this generation) | dream candidate → `dreamrepair:da0ac0f3cab187290c91e5b26a6c5b9f` |
| `task_7c30f939279d411c9ecf7dd41206675e`        | Investigate low-confidence dream finding: slack     | prior generation             | outcome memory `deployment_learning:mac` → next candidate  |
| `task_70962c35128c4314b22f3e15585cb37c`        | Investigate low-confidence dream finding: slack     | prior generation             | outcome memory `deployment_learning:mac` → next candidate  |
| `task_8abb226839e94a4897204e65ec4ed35e`        | Investigate low-confidence dream finding: slack     | prior generation             | outcome memory `deployment_learning:mac` → next candidate  |
| `task_c4415aa9fff540b0828ef6a62563d097`        | Investigate low-confidence dream finding: slack     | prior generation (chain root supplied) | outcome memory `deployment_learning:mac` → next candidate  |

Note: sibling-task state/fingerprint fields live in the MAC hub control plane,
which is not queryable from the investigation sandbox (no `mac` CLI, no hub
read API). The chain shape, titles, and feeding record type are corroborated
against (a) the task's own `metadata.relationships`/`parent_task_*` fields and
(b) the pipeline source that produces the lineage (see mechanism below) and
(c) a corpus of prior identical investigations checked into this repo (below).
Where a specific per-task `state` could not be directly read from the hub, it is
reported as "prior generation" rather than asserted as `failed`; the mechanism
guarantees only that each generation emitted a `deployment_learning:mac` outcome
memory that re-seeds the loop.

## Which Memory Record Type Feeds the Next Candidate

The sole evidence record backing each generation is a
`deployment_learning:mac` outcome/closure memory (schema
`mac.deployment_learning.v1`) produced by the *prior* investigation task — not a
fresh observation of a Slack failure. This is confirmed in current source:

- `src/mac/executor_memory.py:362` `build_learning_record()` stamps the prior
  task's `title` verbatim into a `mac.deployment_learning.v1` record with
  `record_type = "deployment_learning:<project>"`.
- `src/mac/nap_consolidator.py:113` `_record_observation()` renders that record
  as `"[<outcome>] <task_title> (<evidence_type>)"`, e.g.
  `"[failed] Investigate low-confidence dream finding: slack (investigation)"`.
- `src/mac/nap_consolidator.py:162` `_default_dreamer()` builds the candidate
  summary `"failure pattern for <group>. Supported by 1 memory record(s): [failed]
  ...slack... (investigation)"`; `_confidence_for_records()` maps support = 1 to
  `low` / `0.35` (`nap_consolidator.py:154`).
- `src/mac/dream_cycle_classifier.py:143` `_PROVIDER_PATTERNS` matches
  `\bslack\b` → provider area `"slack"`; `low` → score `0.35`
  (`dream_cycle_classifier.py:87`).
- `src/mac/dream_repair_tasks.py:175` `repair_fingerprint()` hashes the candidate
  `summary` (which embeds the *previous* generation's task id) and prefixes
  `"dreamrepair:"` (`dream_repair_tasks.py:189,193`), so every generation gets a
  NEW fingerprint — defeating dedup and perpetuating the loop.
- `src/mac/dream_repair_tasks.py:58` `file_low_confidence_repair_tasks()` mints a
  new investigation task when `overall_confidence == low` AND an affected
  provider is present — which the slack token always satisfies.

## Genuine Slack Failure vs. Recorded Prior-Task Failure

The supporting record does **not** reflect a genuine Slack provider/integration
failure. It reflects the recorded *outcome* of a prior identical investigation
task. Independent inspection of the repository's real Slack surface confirms no
concrete defect signal:

- The repo *does* contain a real Slack surface:
  `src/mac/_hermes/gateway/platforms/slack.py` (3334 lines),
  `src/mac/_hermes/hermes_cli/slack_cli.py`, `scripts/mac-fetch-slack-secrets.py`,
  `scripts/slack-vault-loader.py`, plus `deploy/hermes/multi-slack-mvp.patch`
  and `deploy/nemoclaw/slack-account.example.json`.
- Slack-surface tests all pass: `tests/test_slack_secrets_fetcher.py`,
  `tests/test_slack_thread_participant_triggers.py`,
  `tests/test_hermes_config_surface_slack_tokens.py` → **11 passed**.
- No unimplemented/broken markers (`TODO`/`FIXME`/`NotImplemented`/`broken`)
  implicate a live fault; the only "bug" mention is a comment noting an
  already-fixed key-construction bug (`slack.py:3145`). Slack API error paths are
  defensively handled via `logger.debug` fallbacks, not unhandled failures.
- The finding names no specific symptom, stack trace, request, or channel — only
  the bare token "slack" — so nothing in the real surface is implicated.

The word "slack" in the candidate resolves to the *prior task's title*, not to
any of these real modules; the regex would match "slack" regardless of whether a
Slack integration existed (prior generations of this loop ran when the repo had
no Slack code at all — see `probe-slack-finding.md`).

## Evidence Gap

- Root signal: a single `deployment_learning:mac` outcome memory of a prior
  identical investigation task, surfaced by a bare `\bslack\b` token match.
- Missing for actionability: any concrete Slack fault (symptom, reproduction,
  error signature, request/response, channel/thread, config), any second
  independent supporting record (support stays at 1 → confidence stays `low`),
  and any named skill/tool/repo_area.
- Hub-read limitation: exact per-task `state` and stored candidate JSON for the
  sibling chain could not be read directly from this sandbox; classification does
  not depend on those values, and the mechanism above fully accounts for the
  finding's shape.

## Recommended Disposition

- **Dismiss** `dreamrepair:da0ac0f3cab187290c91e5b26a6c5b9f` as NOT ACTIONABLE
  for Slack/skill/tool/code repair. No repair or scoped Slack change is warranted.
- The durable fix is upstream in the dream-repair pipeline: stop treating a prior
  investigation's own outcome/closure memory as fresh evidence, and/or stop
  minting provider-repair tasks from bare word-boundary token matches on task
  titles. This corroborates the prior loop analysis in
  `investigation-slack-loop-analysis.md` and the disposition notes under `docs/`.

## Corroborating Prior Investigations (checked into this repo)

Multiple prior generations of this exact loop reached the same verdict, each for
a distinct fingerprint, confirming this is a recurring self-referential pattern:

- `investigation-slack-loop-analysis.md` (mechanism / causal diagram)
- `investigation-slack-finding-6349fdff.md`, `probe-slack-finding.md`
- `adjudicate-slack-finding-17364dcb.md`, `adjudicate-slack-finding-dc51263.md`,
  `gather-evidence-slack-finding-dc51263.md`, `investigation-slack-surface-dc51263.md`
- `docs/investigation-dreamrepair-{394db89d,477446f5,cc1dedb0,71b00e8}-slack.md`,
  `docs/provenance-dreamrepair-77fc3e59-slack.md`,
  `docs/disposition-task-{394db89d,cc1dedb0}-slack.md`

Fleet-generic: no secrets, hostnames, personal paths, tokens, or operator
identities are recorded in this document.
