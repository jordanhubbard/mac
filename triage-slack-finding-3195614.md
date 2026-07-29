# Triage: Actionability of Low-Confidence "slack" Dream Finding

Read-only, investigation-only triage for plan node `triage` of parent audit
`task_e18e9096cb92480796bdfb43cc865dcd` (title: "Investigate low-confidence dream
finding: slack"). This note records ground truth only. It changes **no** `src/`,
`tests/`, `skills/`, `deploy/`, tool, or provider code and performs no repair.
Output is fleet-generic: no secrets, host names, personal paths, or operator
identities. Scope is exactly the task contract: read the single supporting record
and the candidate summary, then decide ONLY whether the finding is actionable.

## Verdict

**NOT-ACTIONABLE — evidence gap: a single low-confidence, self-referential
outcome memory with a bare `\bslack\b` token signal and no named, reproducible
Slack (or skill/tool/provider) defect.**

## 1. Inputs Triaged (as given by the task contract)

- **Supporting record:** `mem_3195614307ff4e8faed6da8c73beba26`,
  `record_type = deployment_learning:mac`, originating from prior task
  `task_af492a5172b14264947875a6b936de34`. Per the task description this is the
  *single* supporting record for the finding (`evidence_count = 1`).
- **Candidate summary:** described by the task contract as "itself a prior slack
  investigation" — i.e. the summary is the outcome/closure of an earlier
  investigation of the same low-confidence "slack" finding, not an independent
  observation of a Slack fault.
- **Parent context:** parent title "Investigate low-confidence dream finding:
  slack"; this triage is the `triage` node that decides actionability only.

## 2. Why It Is Not Actionable (evidence gap)

1. **No named target.** The finding's only "target" is the generic provider label
   `slack`. No Slack module, method, channel binding, API call, notifier, config,
   or stack frame is implicated; there is nothing scoped for a change to fix.
2. **No reproducible failure.** The signal is a bare word-boundary match
   (`\bslack\b`) on task text, not a stack trace, error signature, or failing
   test. Nothing reproduces a defect.
3. **Self-referential, zero net-new evidence.** The single supporting record is a
   prior investigation's own `deployment_learning:mac` outcome memory, and the
   candidate summary is itself a prior slack investigation. The finding therefore
   re-derives itself from its own prior closure rather than from any observed
   Slack fault, adding no independent evidence.
4. **Low by construction, not by diagnosis.** A single record yields the
   deterministic low tier (`0.35`) because support is `< 2`, which encodes
   insufficient evidence, not a diagnosed defect.

## 3. Mechanism Re-Corroborated In-Repo (this node)

The finding's shape is a classifier artifact, confirmed by reading the pipeline
source directly:

- Provider label comes from a bare regex: `(r"\bslack\b", "slack")` in
  `_PROVIDER_PATTERNS` (`src/mac/dream_cycle_classifier.py:143`).
- Confidence is a deterministic function of support: `low` unless support is
  `>= 2` (`_confidence_for`, `src/mac/dream_cycle_classifier.py:233`), with
  `CONFIDENCE_THRESHOLDS["low"] = ("low", 0.35)`
  (`src/mac/dream_cycle_classifier.py:87`).

So both the `slack` label and the `0.35` score are produced by a word match plus
a support counter, not by any diagnosed Slack fault.

## 4. Verification Performed (this node)

- Re-ran the targeted Slack/notifier/communication suite in the task venv:
  `.venv/bin/pytest tests/test_slack_secrets_fetcher.py
  tests/test_slack_thread_participant_triggers.py
  tests/test_hermes_config_surface_slack_tokens.py tests/test_notifier_service.py
  tests/test_communication_service.py -q` -> **107 passed**. The live Slack
  surface is healthy and reproduces no defect.
- Re-read the two cited classifier source locations (section 3); both match.
- Confirmed the finding names no Slack module/tool/skill beyond the generic
  provider label, and that the sole evidence record and candidate summary are
  prior-investigation outcomes (self-referential).
- Confirmed no `src/`, `tests/`, `skills/`, or `deploy/` file was modified; the
  only added artifact is this note.

## 5. Explicit Evidence Gap (for closure)

A single low-confidence (`0.35`, `evidence_count = 1`), self-referential
`deployment_learning:mac` outcome record; a bare `\bslack\b` token signal with no
named skill/tool/provider/repo-area; and no reproducible failure signature.
Confidence reflects support `< 2`, not a diagnosed defect.

**Reopen threshold (Slack track only):** reopen only if the Slack provider
acquires a reproducible failure signature (a real error, stack trace, or failing
test) backed by at least two independent, non-self-referential evidence records.

**Upstream note (out of scope here):** the genuine improvement is a
pipeline/process change — avoid manufacturing provider findings from prior
investigation outcome memories via bare word-boundary matches on task titles.
That is a separate, pipeline-scoped item, not a Slack repair, and is not
implemented by this investigation-only task.

## Assumptions Recorded

- The candidate summary, supporting record identity, and evidence-count carried in
  the task/parent contract are authoritative for the finding's content: the hub
  memory/tasks tier is not durably reachable from this sandbox for direct
  dereference of `mem_3195614307ff4e8faed6da8c73beba26` or the parent task
  metadata (the reachable `MAC_HUB_URL` endpoint is an OpenAI-compatible LLM
  gateway, not a memory/tasks API).
- "slack" in the finding is the classifier's generic provider label, not a
  diagnosed fault in a specific Slack module.
