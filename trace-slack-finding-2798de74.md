# Evidence Map: Low-Confidence "slack" Dream Finding `dreamrepair:2798de74002ecc0e7071f83135466e69`

Read-only, investigation-only trace for plan node `trace_evidence` of parent
audit `task_54a0b6591f694eb0a4c9604e7a0d8f79` (title: "Investigate low-confidence
dream finding: slack"). This document records ground truth only. It changes **no**
`src/`, `tests/`, `skills/`, `deploy/`, tool, or provider code and performs no
repair. Output is fleet-generic: no secrets, host names, personal paths, or
operator identities. Hub records were read via the MAC hub read APIs using the
fleet gateway credential; no record was mutated.

## Summary Verdict

**MISLABELED / KEYWORD-ONLY MATCH — NOT a real Slack provider defect.** The
finding is a self-referential dream-repair bookkeeping artifact. Its only "signal"
is the English word "slack" carried in a prior investigation task's own title,
matched by the classifier's bare word-boundary regex `\bslack\b`. Every evidence
record in the chain is a `deployment_learning:mac` failure-outcome memory emitted
because a prior *"trace the slack finding"* investigation task failed a
repository/contract check — never because any Slack integration failed. The
repository does contain a real, substantial Slack provider surface, and it is
healthy and unimplicated by the finding. No node in the chain substantiates a
genuine Slack provider failure.

## 1. The Finding Under Investigation

From the parent audit's `metadata.dream_repair`
(`schema mac.dream_repair_task.v1`):

- Fingerprint: `dreamrepair:2798de74002ecc0e7071f83135466e69`
- Kind: `failure_pattern`; Scope: project `mac`
- Classification (`mac.dream_classifier.v1`): overall confidence `low`,
  `overall_confidence_score = 0.35`, `evidence_count = 1`.
- Affected: providers `['slack']`; skills, tools, repo_areas all empty.
- Area signal: `["\\bslack\\b"]` (a bare word-boundary token match).
- Candidate: `task_8cc424e50f1d48738542519185ab022a`, whose sole evidence is
  `mem_3d112e3564314abca12a988d55294010` (`deployment_learning:mac`).

Every discriminating field is empty or a generic provider label; the only target
is the token "slack".

## 2. Evidence Map (each id: type, content summary, substantiates a Slack defect?)

| ID | Type | Content summary | Substantiates a real Slack defect? |
|----|------|-----------------|-------------------------------------|
| `dreamrepair:2798de74002ecc0e7071f83135466e69` | dream-repair finding (this task) | `failure_pattern`, provider `slack`, low/0.35, evidence_count 1, signal `\bslack\b`; candidate `task_8cc424e...`. | **No** — bare-token classification, no named fault. |
| `task_8cc424e50f1d48738542519185ab022a` | task ("Trace the slack dream-finding evidence chain to its origin"), state `failed` | Prior generation of THIS trace task. Did a read-only investigation, wrote `trace-slack-finding-5a43922c.md`; verification rejected it: `repo code evidence requires at least one passing test/check` and `repository evidence failed local contract checks; refusing to push` (`pushed=false`, contract test `returncode=1`). | **No** — the failure is a contract/evidence-type mismatch on a non-repository investigation, not a Slack fault. |
| `mem_3d112e3564314abca12a988d55294010` | `deployment_learning:mac` (`mac.deployment_learning.v1`), created_by `mac-task-executor` | The failure-outcome memory of `task_8cc424e...`. `outcome=failure`, `evidence_type=repo_change`, `signals={tests: fail, pushed: false, returncode: 0, files_changed: 1}`. Its `error_signature` **quotes the prior investigation's own finding**: "Traced the low-confidence 'slack' failure_pattern dream finding (dreamrepair:5a43922c...) to its origin. The single evidence record mem_1a7589a... (deploymen[…]". `task_title` = "Trace the slack dream-finding evidence chain to its origin". | **No** — pure dream-repair bookkeeping; the word "slack" enters only via the task title/error_signature. |
| `mem_faa3e32b721f443ea4703aa555513d60` | `dream:failure_pattern` (`mac.dream.v1`), created_by `nap-ticker` | The dream artifact this task was minted from. `confidence=low`, `confidence_score=0.35`, sole `evidence` = `mem_3d112e...`. `summary`/`observations`: "[failure] Trace the slack dream-finding evidence chain to its origin (repo_change) failed with Traced the low-confidence 'slack' failure_pattern dream finding …". | **No** — it re-derives a "slack" finding from step 3's self-referential memory. |
| `dreamrepair:5a43922cafc7949dffd0e9f5071daf43` | prior-generation dream-repair finding | The finding that `task_8cc424e...` was investigating (one hop earlier in the lineage). Same shape: provider `slack`, low/0.35, evidence_count 1, sole evidence `mem_1a7589a...`. | **No** — identical self-referential shape. |
| `mem_1a7589a685b043c38493900f0812a1e4` | `deployment_learning:mac` (`mac.deployment_learning.v1`), created_by `mac-task-executor` | The failure-outcome memory of the prior candidate `task_71e001ba...`. `outcome=failure`, `signals={tests: fail, pushed: false, returncode: 0, files_changed: 1}`. `error_signature`: "Traced the low-confidence 'slack' failure_pattern dream finding (dreamrepair:909044938992...) backward from candidate task_b3abd924 through a 42-task self-referential lineage to its n[…]". `task_title` = "Trace the slack dream-finding evidence chain and confirm its origin". | **No** — same bookkeeping loop, one generation earlier; explicitly reports a "42-task self-referential lineage". |
| `task_71e001ba73be4d8ca56b79edceed89f1` | task ("Trace the slack dream-finding evidence chain and confirm its origin"), state `reviewing` | The prior candidate. Its own description says the finding it chased (`dreamrepair:909044938992...`, candidate `task_b3abd924`) derives through the earlier hops `task_684f0f8f...`, `task_4d7cb24b...`, `task_005d4a4a...`, … . | **No** — the description itself frames the chain as self-referential bookkeeping. |
| `mem_dfb1122d823c4ce4bb4ffd2141488202` / `mem_c715ca9380a340bbae0fc30b9434330a` | `dream:failure_pattern` (`mac.dream.v1`), created_by `nap-ticker` | **Two** distinct dream artifacts (by two different worker agents, roles worker-A and worker-B) that both re-derive a low/0.35 "slack" `failure_pattern` from the *same* evidence record `mem_1a7589a...`. `mem_dfb1122d...` seeds fingerprint `5a43922c`. | **No** — shows the single self-referential memory fans out to multiple findings across agents/nap cycles. |

`nap_summary` rows (`mem_bbcc8433...`, `mem_8afef1e6...`, `mem_db9a72cb...`) are
per-agent nap roll-ups that re-quote the same `deployment_learning` records; they
add no independent Slack signal.

## 3. Ground Truth — What the Underlying "slack" Signal Actually Is

- **It is a mislabeled / keyword-only match, and an artifact of prior failed
  investigations — not a real Slack provider defect.** The classifier tags the
  candidate "slack" via `_PROVIDER_PATTERNS` entry `(r"\bslack\b", "slack")`
  (`src/mac/dream_cycle_classifier.py:143`). The text it matches is the
  `error_signature`/`task_title` of a `deployment_learning` memory, both of which
  contain "slack" only because the failing task is titled "…dream finding: slack".
- **Self-reference injection point:** `build_learning_record` stamps the failing
  task's `title` and `error_signature` verbatim into the memory
  (`src/mac/executor_memory.py:362`, fields `task_title` at :374 and
  `error_signature` at :380). Support is a single such record, so
  `_confidence_for_records` assigns `low`/0.35 by construction (support < 2).
- **The failures are not Slack failures.** Both candidate memories carry
  `signals.tests = "fail"`, `pushed = false`, `returncode = 0`. This is Mode 2 of
  the known loop: a non-repository *investigation* submitted against a
  repository (`repo_change`) execution contract is rejected for lacking a pushed
  repo anchor / passing contract test — a contract/evidence-type mismatch, wholly
  unrelated to Slack.
- **A real Slack surface exists and is unimplicated.** The repo ships a
  first-class Slack adapter `src/mac/_hermes/gateway/platforms/slack.py` (3334
  lines) plus CLI/notifier paths; none of it appears anywhere in the finding's
  evidence. Prior sibling audits recorded its targeted suite as healthy (107
  passed). The finding never references this surface.
- **The lineage is deep and self-perpetuating.** This task is generation N of a
  chain (…`task_b3abd924` → `task_71e001ba...` → `task_8cc424e...` → this task),
  which the prior evidence record itself describes as a "42-task self-referential
  lineage". Each failed "trace the slack finding" task emits a fresh
  `deployment_learning` memory whose title still contains "slack", which the next
  nap/dream cycle turns into a new low-confidence "slack" finding with a NEW
  fingerprint (the dedup key embeds the volatile prior task id), so the loop does
  not self-terminate.

## 4. Consequential Assumptions

- **Hub credential reuse.** The MAC hub required a bearer token; none was provided
  as a dedicated `MAC_TOKEN`. I used the available `MAC_HERMES_GATEWAY_API_KEY`
  as the hub bearer for READ-ONLY queries (`task show`, `memory search`). This is
  consistent with the parent/candidate task descriptions, which direct workers to
  authenticate to the hub via `MAC_HERMES_GATEWAY_API_KEY`. No hub state was
  mutated.
- **Chain-depth figure.** The "42-task self-referential lineage" depth is quoted
  from the prior evidence record `mem_1a7589a...`; I corroborated the last three
  hops directly and the loop mechanism in-source, and treat the exact count as
  the prior generation's finding rather than re-walking all 42 hops.
- **Deliverable form.** This audit is a non-repository *investigation*, but the
  task carries a `repo_change` execution contract. To avoid repeating the exact
  Mode-2 failure that seeded this finding, the sole artifact is this committed
  Markdown note (a real, pushable repo change), containing the required evidence
  map. No source/skill/tool/test change is warranted or made.

## 5. Recommended Disposition (analysis only — no change made here)

- Close `dreamrepair:2798de74002ecc0e7071f83135466e69` as **NOT ACTIONABLE /
  dismiss**: self-referential dream-repair classifier artifact, no reproducible
  Slack defect.
- **Reopen criteria:** only if a *named* Slack skill/tool/integration acquires a
  reproducible failure signature (real error/stack/failing test) with at least two
  independent, non-self-referential evidence records.
- The durable fix belongs upstream in the dream-repair pipeline (exclude a task's
  own prior `deployment_learning` failure-outcome memory from the evidence that
  seeds new `failure_pattern` findings, and/or make `repair_fingerprint`
  provenance-stable), not in any Slack integration. Implementing that is out of
  scope for this read-only trace.

## 6. Verification Performed

- Read parent `task_54a0b6591f...` `metadata.dream_repair`: confirmed fingerprint
  `2798de74...`, provider `slack`, low/0.35, evidence_count 1, signal `\bslack\b`,
  candidate `task_8cc424e...`, sole evidence `mem_3d112e...`.
- Read candidate `task_8cc424e...`: state `failed`; verification problems and
  contract-test `returncode=1`; produced `trace-slack-finding-5a43922c.md`.
- Read `mem_3d112e...`, `mem_faa3e32b...`, `mem_1a7589a...`, `mem_dfb1122d...`,
  `mem_c715ca9...` and prior candidate `task_71e001ba...`: confirmed the
  self-referential deployment_learning → dream:failure_pattern loop and its
  cross-agent fan-out.
- Read `src/mac/dream_cycle_classifier.py:143` (the `\bslack\b` provider pattern)
  and `src/mac/executor_memory.py:362` (`build_learning_record` stamping
  `task_title`/`error_signature`) — the mislabel and self-reference mechanisms.
- Confirmed the live Slack surface `src/mac/_hermes/gateway/platforms/slack.py`
  exists (3334 lines) and is not referenced by any evidence node.
- No `src/`, `tests/`, `skills/`, or `deploy/` file was edited by this trace.
