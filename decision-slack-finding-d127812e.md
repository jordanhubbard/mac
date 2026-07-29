# Actionability Decision: Low-Confidence "slack" Dream Finding `dreamrepair:d127812e01d62630fa16797756580276`

Final decision record for plan node `decision` of parent audit
`task_b04d1726fa6e4486b8a5154d0037d356` (title: "Investigate low-confidence dream
finding: slack"). This note synthesizes the two upstream distinct-agent inputs —
the `ground_truth` record (`investigation-slack-finding-d127812e.md`) and the
`surface_analysis` record (`investigation-slack-surface-d127812e.md`) — into a
single actionability verdict and close-out. It applies the parent acceptance
criteria and changes **no** `src/`, `tests/`, `skills/`, or `deploy/` behaviour;
this note is the only artifact. Output is fleet-generic: no secrets, host names,
personal paths, or operator identities.

## Verdict

**NOT ACTIONABLE.** Close `dreamrepair:d127812e01d62630fa16797756580276` as a
slack repair.

- **Decision:** No skill, tool, code, or config repair is warranted for the
  finding as stated. No smallest-repair is available because there is no defect;
  the appropriate output is close-out plus a scoped, out-of-scope follow-up for
  the upstream pipeline.
- **Confidence in the verdict:** High — the two independent upstream inputs agree,
  the finding carries low intrinsic confidence (`0.35`, `evidence_count=1`), and
  the real slack surface is green under test.
- **Class:** Self-referential dream-repair loop artifact, not a slack defect.

## Synthesis Of Upstream Inputs

Both distinct-agent inputs reach the same disposition from independent angles, so
the decision is a consensus, not a tie-break:

- **Ground truth (`investigation-slack-finding-d127812e.md`):** the finding is a
  low-confidence (`0.35`, `evidence_count=1`) `failure_pattern` whose single
  evidence record `mem_c052b78e73dc46eca1c9761fe10f6a84`
  (`deployment_learning:mac`) is the *outcome/closure memory of a prior
  investigation task* (`task_47c7e6938bcb4e7797745f4394486244`) that failed at
  executor startup (`failure_class=environment`, `error_signature=""`, only
  `returncode=1`). The `slack` provider label comes from a `\bslack\b`
  word-boundary match on that prior task's own title (via
  `src/mac/dream_cycle_classifier.py` `_PROVIDER_PATTERNS`), i.e. an incidental
  keyword match, not a causal attribution. No skill, tool, repo area, or file is
  named.
- **Surface analysis (`investigation-slack-surface-d127812e.md`):** the actual
  slack surface the label would implicate is present and coherent — provider
  config (`hermes_config_surface.py`, `gateway/platforms/slack.py`), notifier /
  messaging paths (`notifier_service.py`, `communication_service.py`,
  `send_message_tool.py`), and the slack CLI (`slack_cli.py`) — and it is green:
  the slack-focused subset passes 107/107, and the sole broader-run failure is an
  unrelated coding-agent CLI probe, not a slack code path. The slack platform
  adapter imports cleanly with graceful SDK-optional degradation
  (`SLACK_AVAILABLE=False`). No reproducible slack error signature was produced.

The two inputs are complementary and non-conflicting: ground truth shows the
finding's evidence is self-referential and its label incidental; surface analysis
shows there is no code/config defect for that label to attach to. Nothing in
either input describes a reproducible slack failure.

## Applying The Parent Acceptance Criteria

- **(a) Actionable → smallest repair or scoped follow-up:** The finding is *not*
  actionable as a slack repair (no defect to fix), so no code/skill change is
  made. The only genuinely actionable improvement lives upstream in the
  dream-repair pipeline and is captured as a scoped follow-up below.
- **(b) Not actionable → close with reason and specific evidence gap:** This is
  the operative branch. The reason and the specific evidence gap are recorded
  below; the finding is closed as NOT ACTIONABLE.

## Confidence Assessment (Why Low Is Correct)

The finding is low-confidence by construction, and that assessment is justified,
not a labeling accident:

- **Support < 2:** `evidence_count=1`. The single record is the prior task's own
  failure-closure memory, so net-new independent signal is effectively zero. The
  `0.35` score encodes support-count = 1 and a single signal type, not a diagnosed
  fault.
- **No named target:** affected skills / tools / repo_areas are empty; the only
  "target" is the provider label `slack`, assigned by a bare `\bslack\b` token
  match on prior task text.
- **No reproducible failure:** `error_signature=""`, no stack trace, no failing
  slack test. The one non-null signal is `returncode=1` from an executor-startup
  environment failure.
- **Surface is green:** the real slack code/config surface passes its targeted
  tests, so even the implicated area shows no defect to raise confidence.

Because no affected skills/tools/repo-areas are named and the lone record is
self-referential, the low-confidence classification is exactly what the shape
warrants; nothing observed supports promoting it.

## Named Evidence Gap (Basis For Close-Out)

- **Single self-referential `deployment_learning` record.** `evidence_count=1`,
  and that record is a prior investigation's own outcome/closure memory
  (`mem_c052b78e...`) — not an independent slack failure observation.
- **Empty error signature.** No stack trace, no failing test, no dated slack
  incident; only `returncode=1` from an environment/executor-startup failure.
- **No independent corroboration.** No second, non-self-referential record
  describing a concrete slack failure exists, so the finding cannot rise above
  `low` by construction.

## Scoped Follow-Up (Out Of Scope To Apply Here)

The actionable defect is upstream in the dream-repair pipeline, not in any slack
integration. A future non-planning task should:

- **Target:** the dream-repair candidate/classification path that (a) labels a
  provider from a bare word-boundary match on prior task titles
  (`src/mac/dream_cycle_classifier.py` `_PROVIDER_PATTERNS`) and (b) re-files the
  `deployment_learning` outcome memories of a finding's own prior investigation
  tasks as fresh evidence.
- **Change (proposed):** exclude a finding's own lineage outcome memories from its
  evidence set (break self-referential seeding), and/or make the dedup fingerprint
  stable across generations by excluding volatile prior task ids/titles from the
  hashed material so the lineage collapses instead of regenerating fresh
  fingerprints.
- **Verification:** add a regression test asserting that a lineage whose only
  support is its own prior investigation outcome memory does not spawn a new
  distinct-fingerprint dream-repair task, and that provider labeling requires more
  than a bare title token match.

## Reopen Criteria

Reopen `dreamrepair:d127812e01d62630fa16797756580276` only if a **named** slack
tool/skill/integration acquires a reproducible failure signature (a real
error/stack/failing test naming a slack code path) backed by at least two
independent, non-self-referential evidence records.

## Verification Performed

- Read both upstream inputs (`investigation-slack-finding-d127812e.md` ground
  truth; `investigation-slack-surface-d127812e.md` surface analysis) and confirmed
  they converge on the same NOT ACTIONABLE disposition without conflict.
- Confirmed the finding's claim fields (support 1, confidence `0.35`, signal
  `\bslack\b`, empty error signature, evidence `mem_c052b78e...`, prior task
  `task_47c7e6938...`) as reported by ground truth, and the slack surface being
  green as reported by surface analysis.
- Ran the declared contract test `scripts/run-contract-tests.sh`; results recorded
  in `mac-evidence.json`.
- No `src/`, `tests/`, `skills/`, or `deploy/` file was edited by this
  planning-scoped decision.

## Assumptions Recorded

- The provenance IDs and claim fields supplied by the upstream `ground_truth` and
  `surface_analysis` inputs are authoritative for the memory record, prior task,
  and classifier behaviour, given the hub/memory control plane is not directly
  reachable from this sandbox.
- This sandbox baseline checkout is behind canonical `main`; exact line references
  from the upstream inputs were taken as reported rather than re-derived. The
  decision does not depend on those line numbers — it rests on the finding's
  low-confidence, single self-referential-record shape and the green slack
  surface.
- "slack" in the finding is the classifier's generic provider label, not a
  diagnosed fault in a specific slack module.
