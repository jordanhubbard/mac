# Investigation: Slack Provider Surface vs. Low-Confidence Dream Finding `dreamrepair:dc51263259fbcd75e4ed0d02cae0397b`

Read-only, investigation-only note for plan node `analyze_slack_surface` of the
parent audit `task_709c68a0b39441478cf17cf2bbc944d9`
(title: "Investigate low-confidence dream finding: slack"). Builds on the
dependency node `gather_evidence`
(`gather-evidence-slack-finding-dc51263.md`). This document records ground truth
only. It changes **no** `src/`, `tests/`, `skills/`, `deploy/`, tool, or provider
code, and performs no repair. Output is fleet-generic: no secrets, hostnames,
personal paths, or operator identities.

## Summary Verdict

**NO CONCRETE SLACK DEFECT — NOT ACTIONABLE.** The mac repository has a real,
substantial, and healthy Slack provider surface, but the finding does **not** map
to any part of it. The finding is a self-referential classifier artifact: a
low-confidence (0.35, `evidence_count=1`) `failure_pattern` whose only "target" is
the bare provider label `slack`, produced by a word-boundary regex (`\bslack\b`)
matching the word "slack" in the parent audit's own title, and backed by a single
prior-investigation outcome memory rather than any observed Slack fault. A single
0.35 record with a bare-token signal is insufficient signal to justify any code,
tool, or skill change, and the pattern is a one-off self-loop, not a generalizable
defect class. The live Slack surface passes its full targeted test suite
(107 passed). Correct disposition: close as NOT ACTIONABLE for Slack repair.

## 1. The Slack Provider Surface Actually Exists and Is Healthy

Contrary to a "missing/broken provider" reading, the repository contains a
first-class, actively tested Slack integration. Key surfaces located by read-only
inspection:

- Gateway platform adapter: `src/mac/_hermes/gateway/platforms/slack.py` (3334
  lines) — a full slack-bolt / Socket Mode adapter handling channel and DM
  messages, slash commands, and thread support, with graceful `ImportError`
  fallback (`SLACK_AVAILABLE`) when `slack_bolt`/`slack_sdk` are absent
  (`src/mac/_hermes/gateway/platforms/slack.py:22`).
- CLI surface: `src/mac/_hermes/hermes_cli/slack_cli.py` and Slack handling in
  `src/mac/_hermes/hermes_cli/platforms.py`.
- Notifier / communication layer: `src/mac/notifier_service.py` and
  `src/mac/communication_service.py` carry Slack notification/communication paths.
- Config surface: Slack token handling in the Hermes config surface
  (`src/mac/hermes_config_surface.py`).
- Secrets/vault helpers: `scripts/mac-fetch-slack-secrets.py`,
  `scripts/slack-vault-loader.py`.

Targeted test coverage exists and is green:

- `tests/test_slack_secrets_fetcher.py`
- `tests/test_slack_thread_participant_triggers.py`
- `tests/test_hermes_config_surface_slack_tokens.py`
- `tests/test_notifier_service.py`, `tests/test_communication_service.py`

Running these together: **107 passed** (see section 5). No failing test, error
signature, or reproducible fault surfaced in the Slack provider surface.

## 2. The Finding Does Not Map to This Surface

The finding names no Slack module, method, tool, skill, channel binding, API call,
or stack frame. Its discriminating fields are empty or generic:

- fingerprint: `dreamrepair:dc51263259fbcd75e4ed0d02cae0397b`
- kind: `failure_pattern`; scope: project `mac`; provider: `slack`
- confidence: `low` (`overall_confidence_score = 0.35`); `evidence_count = 1`
- affected skills / tools / repo_areas: none
- signal: `\bslack\b` (a plain word-boundary token match)
- candidate summary (self-referential): `[failure] Investigate low-confidence
  dream finding: slack (investigation)` — the title of a prior investigation task,
  not a diagnosed Slack fault.

There is no linkage — direct or indirect — from the finding to any of the real
Slack files in section 1. The provider label `slack` is the classifier's generic
tag, echoed from the parent task title, not a pointer at
`platforms/slack.py`, `notifier_service.py`, or any other module.

## 3. How the Finding Was Manufactured (provenance corroborated in-repo)

Independent read of the dream-repair pipeline reproduces exactly how a finding of
this shape arises, confirming it is a byproduct of a text match plus a support
counter, not a defect signal:

- Provider labeling: `(r"\bslack\b", "slack")` in `_PROVIDER_PATTERNS`
  (`src/mac/dream_cycle_classifier.py:143`). Any candidate text containing the word
  "slack" — including a prior task title — is labeled `provider=slack`.
- Confidence is a deterministic function of support, not diagnosis. `_confidence_for`
  returns `low` unless there are ≥2 evidence records or ≥2 distinct
  signals/record-types (`src/mac/dream_cycle_classifier.py:233`); `low` maps to
  score `0.35` (`CONFIDENCE_THRESHOLDS["low"] = ("low", 0.35)`,
  `src/mac/dream_cycle_classifier.py:87`). With `evidence_count=1` and one signal,
  the result is `low`/0.35 by construction.
- Task filing: a follow-up task is filed per low finding with title template
  `"Investigate low-confidence dream finding: %s"`
  (`src/mac/dream_repair_tasks.py:345`), which is why the parent task is titled
  "…: slack".
- Fingerprint: SHA-256 over normalized candidate material, truncated to 32 hex
  chars, prefixed `dreamrepair:` (`repair_fingerprint`,
  `src/mac/dream_repair_tasks.py:175`).

Net: the `slack` target and the `0.35` score are artifacts of a bare word-match
and a support counter. The pipeline re-derives the same finding from its own prior
investigation outcome memory — a self-referential feedback loop.

## 4. Single-Record, Low-Confidence Signal Is Not Enough to Justify Change

Against the actionability criterion — *is there a concrete, reproducible defect in
the Slack provider (or a related skill/tool) that a code/skill change would fix?* —
the answer is **no**:

- No reproducible failure: the "signal" is a bare-token text match on a task title,
  not a stack trace, error signature, or failing test.
- Self-referential, zero net-new evidence: the sole record is a prior
  investigation's own outcome/closure memory (`deployment_learning:mac`), so it
  adds no independent observation of a Slack fault.
- Low by construction: 0.35 reflects `evidence_count < 2`, not a diagnosed defect.
- Not generalizable: this is a one-off self-loop keyed on the word "slack" in a
  title, not a recurring, provider-wide failure class. A single 0.35 record is
  below any reasonable bar for editing otherwise-healthy, fully-passing code.

Making a "repair" here would edit healthy Slack code on the basis of a non-defect
and is out of scope for this investigation-only task.

## 5. Verification Performed

- Located the Slack surface across `src/`, `tests/`, `scripts/`, `skills/`, and
  `deploy/` (section 1); confirmed `platforms/slack.py` is a full, guarded adapter.
- Ran the targeted Slack/notifier/communication suite in the task venv:
  `.venv/bin/pytest tests/test_slack_secrets_fetcher.py
  tests/test_slack_thread_participant_triggers.py
  tests/test_hermes_config_surface_slack_tokens.py tests/test_notifier_service.py
  tests/test_communication_service.py -q` → **107 passed**. No Slack defect
  reproduced.
- Corroborated the finding's provenance directly in the pipeline source:
  `\bslack\b` provider pattern (`src/mac/dream_cycle_classifier.py:143`),
  support-based confidence (`src/mac/dream_cycle_classifier.py:233`,
  `src/mac/dream_cycle_classifier.py:87`), the low-finding task-title template
  (`src/mac/dream_repair_tasks.py:345`), and fingerprinting
  (`src/mac/dream_repair_tasks.py:175`).
- Confirmed no `src/`, `tests/`, `skills/`, or `deploy/` file was modified by this
  investigation; the only added artifact is this note. Worktree otherwise clean.

## 6. Disposition and Reopen Criteria

- **Decision:** NOT ACTIONABLE as a Slack repair. Close
  `dreamrepair:dc51263259fbcd75e4ed0d02cae0397b`. This matches and corroborates the
  `gather_evidence` verdict for the same fingerprint.
- **The real, upstream issue is a pipeline artifact, not a Slack change:** the
  dream-repair loop labels a provider from a bare `\bslack\b` word match on a prior
  investigation's title and treats that prior task's own outcome memory as fresh
  evidence. Any remediation belongs to the pipeline/process, not to the Slack
  provider, skills, or tools — and is out of scope here.
- **Reopen criteria (Slack track only):** reopen only if the Slack provider
  acquires a reproducible failure signature (a real error, stack trace, or failing
  test) backed by at least two independent, non-self-referential evidence records.

## Assumptions Recorded

- The candidate summary, provider label, confidence, and evidence-count supplied in
  the task/dependency contract are authoritative for the finding's content, given
  the hub/memory tier is not durably reachable from this sandbox for direct memory
  dereference.
- "slack" in the finding is the classifier's generic provider label, not a
  diagnosed fault in a specific Slack module.
