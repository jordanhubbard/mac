# Ground-Truth Finding: dream-cycle "slack" failure_pattern `dreamrepair:357fa41858e0376949ed335fb913b59b`

Investigation-only. This document establishes ground truth and changes **no**
`src/`, `tests/`, `skills/`, `deploy/`, or other product code. No skill, tool,
provider, or configuration is repaired. Evidence type: `investigation`.

Parent audit: `task_b6da52635df94dc6845aff819eb74b23` ("Investigate low-confidence
dream finding: slack"), plan node `evidence_review`.

## Verdict

**(b) Process/finalization failure, mislabeled to provider `slack` by keyword.**

The failure was **not** caused by anything Slack-specific. The single supporting
record describes a worker-side finalization/evidence-assembly failure (the
executor succeeded, but the worker could not produce the required repository
evidence). The `slack` provider label is an artifact of a bare word-boundary
keyword match (`\bslack\b`) against task text that merely contained the word
"slack" — inherited from the recursive "Investigate low-confidence dream finding:
slack" lineage — not a diagnosed Slack fault.

## The Finding's Claim

- fingerprint: `dreamrepair:357fa41858e0376949ed335fb913b59b`
- kind: `failure_pattern`; scope: project `mac`
- affected providers: `["slack"]` — a bare provider token, no named target
- confidence: `0.35` (low); `evidence_count = 1`
- classification signal: `\bslack\b` — a plain word-boundary regex match on the
  token "slack" in the task text; the sole discriminating field. All other
  discriminating fields (skills, tools, repo_areas) are empty.

A `failure_pattern` with support = 1 and a bare-token signal is exactly the shape
the dream-cycle heuristics score lowest; `0.35` reflects support < 2, not a
diagnosed defect.

## The Single Supporting Record — What It Actually Says

- memory record: `mem_def69b06c56a42a98e2aea0b0342fd36`,
  record_type `deployment_learning:mac` (schema `mac.deployment_learning.v1`).
- candidate task: `task_33522103ecdf4d459c95e81d98b4dd8d`
  ("Audit whether project mac has real Slack integration surface").
- recorded failure reason (error signature):
  **"worker finalized missing repository evidence for successful executor result".**

That reason string is emitted verbatim by the worker's finalization path in
`src/mac/worker.py:4328`, as the `summary` of a `repo_change` verification
manifest. It is produced when the coding executor returned a **successful**
result but the worker finalizer could not assemble/push the required repository
evidence (`repo.head_sha`, `repo.pushed`, `repo.dirty`, `repo.files_changed`,
`tests`). It is a pure hub/worker finalization outcome. It has **no** Slack code
path, Slack API call, Slack credential, or Slack channel in its causal chain.

The `deployment_learning:mac` record itself is written by the hub-review workflow
(`_record_review_outcome_lesson` / `_record_project_failure_lesson` in
`src/mac/services.py`), whose content carries `evidence_type`, `outcome`,
`error_signature`, and `signals` — none of which are provider-specific. The
`slack` label is applied downstream by the classifier keyword match, not stored
as a Slack observation.

## Why "slack" Was Attached (the exact signal)

- The candidate task's own title contains the word "Slack"
  ("Audit whether project mac has real Slack integration surface").
- The dream-repair classifier maps text to a provider via a bare keyword pattern,
  historically `(_PROVIDER_PATTERNS)` entry `(r"\bslack\b", "slack")`. The token
  "slack" in the task title matched `\bslack\b`, so the finalization-failure
  outcome memory was re-tagged provider = `slack`.
- This is a text match on a title, not a diagnosed Slack fault. Treating the prior
  task's own outcome memory as fresh "slack" failure evidence is the same
  self-referential dream-repair feedback loop documented for sibling findings in
  this repo (e.g. `investigation-slack-finding-6349fdff.md`,
  `gather-evidence-slack-finding-dc51263.md`).

## Slack Surface Note (candidate task context)

The candidate task asked whether project `mac` has a real Slack integration
surface. Read-only inspection shows `mac` does have a Slack **notification
channel** surface (e.g. `src/mac/notifier_service.py` supports `slack` alongside
`hermes`/`telegram`; `src/mac/deploy_env.py` passes through `SLACK_*` /
`MAC_HERMES_SLACK_*` env). However, that surface is a downstream notification
outbox, **not** a coding/executor provider, and it is **not** implicated by this
finding. The failure record is about worker finalization, independent of any
Slack channel.

## Ground-Truth Summary

- The failure is a **process/finalization failure**: executor succeeded, but no
  repository evidence was produced/published by the worker finalizer.
- The `slack` classification is a **keyword mislabel**: driven solely by the
  `\bslack\b` word-boundary match on task text mentioning Slack.
- Classification confidence is **0.35** with **evidence_count = 1** — the
  lowest-support shape, consistent with a non-actionable, keyword-only artifact.
- Correct disposition: **NOT ACTIONABLE as a Slack repair.** The single record is
  a finalization outcome memory, not a Slack observation. Any genuine remediation
  belongs to the worker/finalizer evidence path and to the dream-repair keyword
  classifier's self-referential loop — not to any Slack provider, skill, or tool.
