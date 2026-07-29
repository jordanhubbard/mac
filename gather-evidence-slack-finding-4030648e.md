# Gather Evidence: Low-Confidence "slack" Dream Finding `dreamrepair:4030648e229fc21d4fec6af0038e7a4f`

Read-only, investigation-only evidence collection for plan node `gather_evidence`
of parent audit `task_ee1e24f044b9453aaf74fdd42e297984`
(title: "Investigate low-confidence dream finding: slack"). This note records
ground truth only. It changes **no** `src/`, `tests/`, `skills/`, `deploy/`, or
config file and performs no skill or tool repair. Output is fleet-generic: no
secrets, host names, personal paths, or operator identities.

## Summary Verdict

**EVIDENCE GAP — the finding is a self-referential classifier artifact, not a
demonstrated Slack failure.**

The finding is a low-confidence (`0.35`, `evidence_count=1`) dream-repair
`failure_pattern` whose only "target" is the generic provider label `slack`.
That label is produced by a bare word-boundary regex (`\bslack\b`) matching the
word "slack" that appears in the parent audit's own title ("Investigate
low-confidence dream finding: slack"). Its single supporting evidence record
(`mem_dfdac60d64204db39231f26d7e556b61`, `record_type
deployment_learning:mac`) and originating task
(`task_f1fb6436275b4862b275feedb9455fa3`, title "Investigate low-confidence
dream finding: slack (investigation)") are closure/outcome memory of a prior
investigation stub — not an observation of a broken Slack integration surface,
tool, or skill. The finding lists NO skills, NO tools, and NO repo_areas, which
is consistent with a provider-label-only match rather than a diagnosed fault.

No named Slack tool, skill, API call, channel binding, delivery error, or stack
trace is implicated by the finding. On the evidence available in this read-only
worktree, the correct disposition for the parent audit is: **NOT ACTIONABLE for
skill/tool/code repair; close as a classifier/provenance artifact.**

## 1. The Finding's Claim (as given in the task)

- fingerprint: `dreamrepair:4030648e229fc21d4fec6af0038e7a4f`
- kind: `failure_pattern` (dream-repair low-confidence finding)
- scope: project `mac`; provider: `slack`
- confidence: `low` (overall_confidence_score `0.35`)
- evidence_count: `1`
- affected skills: none; affected tools: none; affected repo_areas: none
- supporting memory: `mem_dfdac60d64204db39231f26d7e556b61`
  (`record_type deployment_learning:mac`)
- originating task: `task_f1fb6436275b4862b275feedb9455fa3`
  ("Investigate low-confidence dream finding: slack (investigation)")

Every discriminating field is a placeholder or an echo of the investigation
itself. The provider label `slack` is the only signal, and the originating task
title is literally the title template of a prior low-confidence investigation —
not a diagnosed fault in any Slack code path.

## 2. Provenance of the Finding (how a finding of this shape is manufactured)

Read-only inspection of the dream-repair pipeline in this worktree establishes
exactly how this finding shape is produced:

- Provider labeling is a bare regex. `_PROVIDER_PATTERNS` includes
  `(r"\bslack\b", "slack")` (`src/mac/dream_cycle_classifier.py:143`). Any
  candidate text containing the word "slack" — including a prior task title —
  is labeled `provider=slack` (`src/mac/dream_cycle_classifier.py:324`).
- Confidence is a pure function of support count, not defect severity. `low`
  maps to score `0.35` and is documented as "Signal is present in the artifact
  text, but only a single evidence record backs it"
  (`src/mac/dream_cycle_classifier.py:14`, `CONFIDENCE_THRESHOLDS["low"] =
  ("low", 0.35)` at `src/mac/dream_cycle_classifier.py:87`). `evidence_count < 2`
  → `low` (`src/mac/dream_cycle_classifier.py:258`). So `0.35` encodes
  support=1, not a diagnosed Slack failure.
- The follow-up task title comes from affected labels, falling back to the
  provider bucket. `_task_title` joins `skills + tools + providers +
  repo_areas`; with only a provider match it renders `... : slack`
  (`src/mac/dream_repair_tasks.py:332`, provider bucket at
  `src/mac/dream_repair_tasks.py:243`). This explains why the finding carries a
  `slack` provider label but empty skills/tools/repo_areas.
- Low-confidence findings are auto-filed as follow-up investigation tasks
  (`file_low_confidence_repair_tasks`, `src/mac/dream_repair_tasks.py:58`;
  origin type `dream_low_confidence_repair`,
  `src/mac/dream_repair_tasks.py:18`), which is how the parent audit and this
  child gather-evidence node came to exist.

Consequence: a finding can be minted whose entire "Slack" signal is the word
"slack" appearing in a prior investigation's own title, with a single closure
memory as its lone evidence record. That is the shape matched here.

## 3. Requested Records (retrieval status and gap)

The task asks to retrieve and summarize:
- memory record `mem_dfdac60d64204db39231f26d7e556b61`
  (`record_type deployment_learning:mac`), and
- originating task `task_f1fb6436275b4862b275feedb9455fa3`.

**GAP — these records live in the live MAC control-plane memory/task store, not
in this repository.** The prepared task worktree is a read-only git checkout; it
contains the memory *service* code (`src/mac/memory_service.py`,
`get_memory`/`search_memory` at `src/mac/memory_service.py:93`,
`src/mac/memory_service.py:99`) but no populated store, DB, or export of these
IDs. A repository-wide search for the literal IDs
(`mem_dfdac60d64204db39231f26d7e556b61`, `task_f1fb6436275b4862b275feedb9455fa3`,
and the fingerprint `4030648e229fc21d4fec6af0038e7a4f`) returns no matches.

What can be asserted about them from schema + task metadata (facts, not the
record bodies):
- `record_type deployment_learning:mac` is an outcome/closure memory class, i.e.
  the memory written when a task completes — consistent with the finding's lone
  evidence being an investigation stub's closure record, not a fault observation.
- The originating task title ("Investigate low-confidence dream finding: slack
  (investigation)") is itself an instance of the low-confidence follow-up title
  template (Section 2), confirming the self-referential loop: an investigation
  task's own artifacts became the evidence for a new "slack" finding.

Downstream steps with control-plane access should fetch both records to confirm
there is no embedded Slack error/stack trace. On current evidence there is none
to be found in the repo.

## 4. Slack Provider Integration Surface (for later assessment)

Because the finding names no concrete area, the following inventory is provided
so a later step can confirm nothing here is implicated. These are the
Slack-touching surfaces in the `mac` project (read-only inventory only):

- Gateway platform adapter: `src/mac/_hermes/gateway/platforms/slack.py`
  (slack-bolt Socket Mode; receive/send/slash/threads). Import-guarded by
  `SLACK_AVAILABLE`.
- CLI surface: `src/mac/_hermes/hermes_cli/slack_cli.py`
  (`hermes slack manifest` — generates the Slack app manifest JSON).
- Notifier delivery: `src/mac/notifier_service.py`
  (`SUPPORTED_CHANNEL_TYPES = {"hermes", "slack", "telegram"}` at
  `src/mac/notifier_service.py:59`; slack channel-id normalization at
  `src/mac/notifier_service.py:387`; openclaw outbox routing at
  `src/mac/notifier_service.py:310`).
- Config/target shape: `src/mac/config_flags.py:11`
  (documents the `slack:C123` `platform:chat_id` binding shape).
- Secret/vault helpers: `scripts/mac-fetch-slack-secrets.py`,
  `scripts/slack-vault-loader.py` (credential loading; not exercised by this
  finding).

None of these surfaces is referenced, named, or error-linked by the finding.
The finding's provider label is the only connection, and it is a text match, not
a code/config pointer.

## 5. Concrete Facts vs. Gaps

Facts (verified in this worktree):
- The finding's `slack` label originates from a word-boundary regex
  (`src/mac/dream_cycle_classifier.py:143`).
- Confidence `0.35` == support-count `low`, not severity
  (`src/mac/dream_cycle_classifier.py:87`, `:258`).
- Empty skills/tools/repo_areas is consistent with a provider-only match and the
  title fallback in `src/mac/dream_repair_tasks.py:332`.
- A real Slack integration surface exists (Section 4) but is untouched by the
  finding.

Gaps / ambiguities (flagged):
- The referenced memory record and originating task cannot be retrieved from
  this read-only worktree; their bodies are unverified here. A control-plane
  fetch is required to positively rule out an embedded Slack error.
- `deployment_learning:mac` record_type strongly implies a closure/outcome
  memory rather than a fault observation, but this is inferred from schema, not
  read from the record body.
- No Slack API error, delivery failure, channel-binding fault, or stack trace is
  present anywhere in scope; absence is evidence of a classifier artifact but is
  not a positive proof until the two records are read.

## 6. Recommended Disposition (for the blocking parent audit)

- Treat `dreamrepair:4030648e229fc21d4fec6af0038e7a4f` as **NOT ACTIONABLE** for
  skill/tool/code repair on the evidence available here.
- Have a control-plane-capable step fetch `mem_dfdac60d64204db39231f26d7e556b61`
  and `task_f1fb6436275b4862b275feedb9455fa3` to confirm no embedded Slack
  fault; if confirmed empty, close the parent audit as a
  classifier/self-reference artifact.
- No change to any Slack code, config, skill, or tool is warranted by this
  finding.

## Sources

- `src/mac/dream_cycle_classifier.py` (provider patterns, confidence thresholds)
- `src/mac/dream_repair_tasks.py` (title fallback, low-confidence filing)
- `src/mac/notifier_service.py`, `src/mac/config_flags.py`,
  `src/mac/_hermes/gateway/platforms/slack.py`,
  `src/mac/_hermes/hermes_cli/slack_cli.py` (Slack surface inventory)
- `src/mac/memory_service.py` (memory retrieval API; store not present in worktree)
- task.json metadata (fingerprint, IDs, confidence, evidence_count, parent audit)
