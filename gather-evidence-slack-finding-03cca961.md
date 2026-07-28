# Investigation: Gather Evidence & Assess Actionability — Low-Confidence "slack" Dream Finding `dreamrepair:03cca961f9d7de9c6ca39f8c9912d9c4`

Read-only, investigation-only evidence-gathering for the parent audit task
"Investigate low-confidence dream finding: slack" (plan node `gather_evidence`,
integration parent). This document records ground truth only. It changes **no**
`src/`, `tests/`, `skills/`, or `deploy/` file and makes no skill, tool, or
provider repair. Ground truth was established from the reachable tasks/memory
API (read-only, via the gateway bearer key) and read-only inspection of the
repository worktree. Output is fleet-generic: no secrets, host names, personal
paths, or operator identities.

## Summary Verdict

**NOT ACTIONABLE — evidence gap; no reproducible slack-provider defect exists.**

The finding is a low-confidence (`overall_confidence = low`, score `0.35`,
`evidence_count = 1`) `failure_pattern` whose only "target" is the bare provider
label `slack`, with no affected skill, tool, or repo area. Its sole supporting
record (`mem_3be7be5e17e14454812f105a176fe1d5`) is not an observation of a broken
Slack integration — it is the **failure-outcome memory of a prior investigation
task** (`task_55f82550499a4ea781e12273cbfafce8`, itself titled "Investigate
low-confidence dream finding: slack", state `failed`). The single classifier
signal is the word-boundary regex `\bslack\b`, which matches the word "slack" in
that prior task's own **title**, not any diagnosed fault. This is one link in a
self-referential chain of ≥50 identical "slack" investigation tasks that
manufacture each successive finding from their own prior outcome memories. There
is no concrete, reproducible Slack defect for a code, tool, or skill change to
fix. Recommended disposition: close as NOT ACTIONABLE (dismiss) with the reopen
threshold below. The genuine actionable weakness is upstream in the dream-repair
pipeline, not in any Slack surface in this repository.

## 1. The Finding Under Review

- fingerprint: `dreamrepair:03cca961f9d7de9c6ca39f8c9912d9c4`
- kind: `failure_pattern`; scope: project `mac`
- confidence: `low` (`overall_confidence_score = 0.35`)
- affected providers: `["slack"]`; affected skills / tools / repo_areas: none
- evidence_count: `1`
- signal: `\bslack\b` (a plain word-boundary token match)
- candidate summary: "failure pattern for task=`task_55f82550499a4ea781e12273cbfafce8`
  project=mac. Supported by 1 memory record(s): [failure] Investigate
  low-confidence dream finding: slack (investigation)"

Every discriminating field is empty or a generic label. The sole "signal" is the
English word "slack". A `failure_pattern` with support = 1 and a bare-token signal
is exactly the shape the classifier scores lowest.

## 2. The Single Supporting Evidence Record (Step 1 — retrieved)

Record `mem_3be7be5e17e14454812f105a176fe1d5`
(`record_type = deployment_learning:mac`, `created_by = mac-task-executor`),
attached to `task_id = task_55f82550499a4ea781e12273cbfafce8`. Its content
(schema `mac.deployment_learning.v1`) is the closure record of that task:

- `outcome`: `failure`
- `evidence_type`: `investigation`
- `error_signature`: `""` (empty — **no** captured fault signature)
- `repository`: `""`
- `signals`: `checks_pass=null, files_changed=null, pushed=null, tests=null,
  returncode=1`
- `task_title`: `"Investigate low-confidence dream finding: slack"`

The only place the token "slack" appears in this record is the **task title** it
copied. There is no Slack API error, log line, stack trace, failed request, or
repro — only a generic non-zero return code from a prior investigation that
itself found nothing actionable.

## 3. Origin Task (Step 1 — retrieved)

`task_55f82550499a4ea781e12273cbfafce8` — "Investigate low-confidence dream
finding: slack (investigation)", state `failed`.

- Its own finding fingerprint was `dreamrepair:37ce1da427edbdaed81ca73f695145fd`,
  also `provider=slack`, `confidence=low`, `evidence_count=1`.
- Its own sole evidence was `mem_bab5a8670b844fa2b02e880cfea37cdb` — the failure
  memory of the earlier task `task_b8321b0f7d55409289ad11bc1ce4fff0` (also
  "Investigate low-confidence dream finding: slack", also `failed`, fingerprint
  `dreamrepair:cc48c716982325da2a34b8e3781cf4a1`).
- The task failed with `executor_failed` / `non_retryable_attempt_failure`; its
  own closure produced `mem_3be7be5e...`, which the nap consolidator then
  re-classified (record `mem_4c2c61130947405794da97bbd689031e`,
  `dream:failure_pattern`) into the very finding audited here.

This is a closed self-referential loop: failed slack investigation → failure
memory → nap re-classifies token "slack" → new low-confidence slack finding →
new slack investigation. A hub `tasks/search` for the title returns ≥50 such
tasks (states include `waiting`, `open`, `running`, `failed`).

## 4. Exact Evidence Gap (Step 3)

To be actionable, the finding would need at least one of the following, and has
**none**:

- A non-empty `error_signature` or a captured Slack API / adapter error (rate
  limit, auth/token failure, Socket Mode disconnect, event-handler exception).
- A concrete affected surface: a named skill, tool, module path, or the Slack
  adapter itself — the finding's skills/tools/repo_areas are all empty.
- A reproduction or failing test against the Slack integration.
- Evidence support ≥ 2 from independent records rather than a single self-derived
  outcome memory (the classifier's `low` label reflects support < 2, per
  `_CONFIDENCE_SCORES["low"] = ("low", 0.35)` in `dream_cycle_classifier.py`,
  mirroring `nap_consolidator`).

The provider label was assigned purely because `\bslack\b`
(`dream_cycle_classifier.py`, `_PROVIDER_PATTERNS`) matched the word "slack" in
the prior task title — a text match, not a diagnosed fault.

## 5. Slack-Provider Surface Mapping (Step 4)

The repository does contain a real, substantial, tested Slack integration — so a
genuine defect would have somewhere concrete to map, and this finding maps to
none of it:

- `src/mac/_hermes/gateway/platforms/slack.py` — Slack adapter (~3,334 lines):
  slack-bolt Socket Mode, message send/receive, slash commands, threads;
  gracefully degrades when `slack_bolt` is absent (`SLACK_AVAILABLE = False`).
- `src/mac/_hermes/hermes_cli/slack_cli.py` — Slack CLI surface.
- `scripts/mac-fetch-slack-secrets.py` — Slack secrets fetcher.
- Tests: `tests/test_slack_thread_participant_triggers.py`,
  `tests/test_hermes_config_surface_slack_tokens.py`,
  `tests/test_slack_secrets_fetcher.py`.

Inspection surfaced no defect tied to the finding: the finding carries no error
signature, module, or repro that points at any of these files. The Slack surface
is healthy relative to the (empty) evidence.

## 6. Recommended Disposition

- **Close as NOT ACTIONABLE (dismiss).** No Slack code, tool, or skill change is
  warranted; making one would be unfounded.
- **Reopen threshold:** reopen only if a future finding for provider `slack`
  arrives with a non-empty `error_signature` or a concrete affected
  skill/tool/repo_area **and** evidence_count ≥ 2 from records that are not
  themselves prior "Investigate low-confidence dream finding: slack" outcome
  memories.
- **Upstream note (out of scope for this task):** the durable weakness is the
  dream/nap pipeline re-deriving `failure_pattern` findings from its own
  investigation-outcome memories, which manufactures self-referential loops. Any
  fix belongs there, not in the Slack provider.

## Consequential Assumptions

- `evidence_type`: the task description declares this investigation-only
  (`evidence_type=investigation`, "Do NOT change any skills, tools, or code"),
  which takes precedence over the contract's generic `repo_change` default. The
  only artifact produced is this read-only write-up.
- Hub reachability: ground truth was corroborated live against the reachable
  tasks/memory API using the gateway bearer key (read-only GETs only); no state
  was mutated.
