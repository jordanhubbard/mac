!!! warning "Historical field note"
    This record preserves ground-truth investigation evidence for a single dream
    finding. It is not a current operating contract; use the numbered book and
    current runbooks for instructions.

# Ground truth: dream finding `dreamrepair:4becfa8d` (slack failure_pattern)

**Finding**: `dreamrepair:4becfa8dfd2a06539d4dbb0fb4f53be9`, kind
`failure_pattern`, scope `project`, project `mac`, confidence `low`,
`evidence_count=1`, provider label `slack`.

**Candidate summary (as supplied)**: "failure pattern for
`task=task_4b603d746c2a4359b2c6b5c2ef2ac653` project=mac. Supported by 1 memory
record(s): [failure] Audit slack dream finding evidence and provenance
(repo_change)".

**Sole cited evidence**: `mem_ad60e715d76a41199f256975851d6df8`
(`record_type=deployment_learning:mac`).

**Prepared by**: fleet worker (read-only investigation). No files under
`src/`, `tests/`, `skills/`, or `deploy/` were modified.

## Verdict: NOT ACTIONABLE

The attached evidence does not name a reproducible Slack defect. The `slack`
token is a word in a prior *audit* task title that the nap dreamer copied into
an observation. The sole supporting record is a `deployment_learning:mac`
closure-shaped snapshot for that audit task, with a blank error signature. Hub
lookups that would retrieve the memory row and originating task were
unavailable in this sandbox (`policy_denied` / hub-mode gaps). Nothing in the
current tree implicates a Slack transport, skill, or API as the failing
surface.

## Q1 — Actionability

Does the attached evidence name a reproducible Slack defect (named Slack
tool/skill/API/binding, error signature, failing test, dated incident)?

**No.**

The observation string is exactly the shape
`_record_observation` emits for `mac.deployment_learning.v1` when
`error_signature` is empty (`src/mac/nap_consolidator.py`):

- `[outcome] title (evidence_type)`
- the suffix ` failed with <error_signature>` is appended **only** when
  `error_signature` is non-empty after strip.

The supplied observation is `[failure] Audit slack dream finding evidence and
provenance (repo_change)` with no ` failed with …` suffix. That is a blank
error signature, not a stack, HTTP status, Slack API method, or test id.

### Named Slack surfaces in this tree

| Path named in the task | Present? | Implicated by this finding? |
|---|---|---|
| `src/mac/_hermes/gateway/platforms/slack.py` | **absent** (no `_hermes/` tree) | no |
| `src/mac/_hermes/hermes_cli/slack_cli.py` | **absent** | no |
| `src/mac/notifier_service.py` | present; `slack` is a supported channel type | no — not named in the evidence |
| `src/mac/communication_service.py` | present; `slack` is a channel name in `CHANNELS` | no — not named in the evidence |
| `scripts/mac-fetch-slack-secrets.py` | present (vault → local Hermes env upsert) | no |
| `scripts/slack-vault-loader.py` | present (local tokens → vault) | no |
| `tests/test_slack_*.py` | `tests/test_slack_secrets_fetcher.py` only | no failing test is cited |

Related but not in the named list: `tests/test_hermes_config_surface_slack_tokens.py`
covers token promotion into the Hermes config env block. It is likewise not
referenced by the finding.

**Named evidence gap**: there is no Slack module, skill, API method, binding,
error signature, failing test identifier, or dated incident in the attached
evidence. The only Slack signal is the English word `slack` inside a prior
task title that the default dreamer copies verbatim.

## Q2 — Provenance

Is the single supporting record self-referential (closure/outcome memory of a
prior task in this same investigation lineage) rather than an independent
Slack failure signal?

**Yes, as far as the producer code and the attached strings can prove. Hub
confirmation of the memory row itself was unavailable.**

### Hub lookups (attempted, not inferred)

Read-only `mac` CLI against the configured hub, `--json` where applicable:

- `mac task show task_4b603d746c2a4359b2c6b5c2ef2ac653` → hub GET
  `/tasks/<id>` **not permitted by policy**.
- `mac admin memory search --task-id task_4b603d…` and
  `--record-type deployment_learning:mac` → GET `/memory?…` **not permitted
  by policy**.
- `mac admin dream list` → `list_dream_runs` **not yet supported in hub
  mode**.
- `mac admin memory recall-dreams --kind failure_pattern …` → GET
  `/v1/memory/dreams/recall?…` **not permitted by policy**.
- Direct `GET /health` on the hub URL → **not permitted by policy**.

Those lookups are therefore **unavailable**, not used as positive evidence.

### What the producer code does prove without the hub

`NapConsolidatorService._group_records` keys groups on the record's own
`task_id` (and the task's project). `_group_label` renders
`task=<task_id> project=<project>`. `_default_dreamer` titles the artifact
`"<kind words> for <group_label>"` and prefixes the summary with that title
plus `Supported by N memory record(s):` and up to three
`_record_observation` lines.

The supplied candidate summary matches that template for
`task_4b603d746c2a4359b2c6b5c2ef2ac653` / project `mac` / one observation.
A candidate labelled that way can only be built from records whose
`task_id` is that id.

`record_type=deployment_learning:mac` is the closure lesson type written by:

- `build_learning_record` / `record_deployment_learning` in
  `src/mac/executor_memory.py` (`created_by=mac-task-executor`);
- the hub project-failure lesson in `src/mac/services.py`
  (`created_by=mac-hub-review`, `outcome=failure`).

The observation title is an audit of Slack *dream-finding evidence and
provenance* with `evidence_type=repo_change` — the same investigation
lineage, not a Slack send/auth/webhook incident. Combined with a blank
error signature, the sole evidence is the prior audit's own failure-shaped
learning record, not an independent Slack runtime signal.

## Q3 — Live producer

Which stages of the documented regeneration loop still exist?

Confirmed starting points were re-checked in this tree:

- `src/mac/nap_consolidator.py` **is present**. It still renders prior-task
  titles into dream candidates via `_record_observation`, `_dream_kind`,
  `_confidence_for_records`, and `_default_dreamer`.
- `src/mac/dream_scanner.py`, `src/mac/dream_cycle_classifier.py`, and
  `src/mac/dream_repair_tasks.py` **do not exist**. `src/mac/dreaming/__init__.py`
  states they were replaced by `mac.dreaming` (asynchronous candidate-store
  curation, not a keyword scanner that files repair tasks).
- A search for the string `dreamrepair` under `src/`, `scripts/`, `tests/`,
  `deploy/`, and `skills/` returns **zero** matches.

### Candidate manufacture — live (manual / API path)

`NapConsolidatorService.consolidate_agent` still defaults
`emit_dream_artifacts=True`. When that flag is true it writes
`dream:<kind>` memory rows from `_default_dreamer` (or a caller-supplied
`dreamer_fn`).

That path is still reachable:

- HTTP `POST /agents/{agent_id}/nap-consolidate` (`src/mac/api.py`), body
  default `emit_dream_artifacts=True`.
- CLI `mac admin nap consolidate` (`src/mac/cli.py`), dreams on unless
  `--no-dreams`.

Single-record groups still get confidence `low` / score `0.35`
(`_confidence_for_records`). `_dream_kind` still returns `failure_pattern`
if the concatenated record type/content contains `failure`, `failed`, or
`error` — including a deployment-learning `outcome` of `failure`.

### Candidate manufacture — scheduled path mitigated

`ControlPlane.run_nap_cycle` always calls `consolidate_nap` with
`emit_dream_artifacts=False`, with an inline comment that the legacy
dreamer is replaced by `mac.dreaming`. The systemd unit
`deploy/systemd/mac-nap-tick.service` runs `mac admin nap cycle`, so the
ticker does not emit the old `dream:failure_pattern` rows.

If `run_nap_cycle` is invoked with `emit_dream_artifacts=True`, it runs
`run_dream_cycle` (the new `mac.dreaming` pipeline), not `_default_dreamer`.

### Provider labelling / `dreamrepair:` fingerprinting — gone

No live module assigns `area_name=slack` or mints `dreamrepair:` keys.
Those behaviours lived in the removed classifier / repair-task modules.
`mac.dreaming` does not reproduce that labelling.

### Repair-task filing — gone

`run_nap_cycle` still returns keys `repair_tasks` and `repair_task_error`.
They are initialized empty/None and never written. The comment in
`src/mac/services.py` states the low-confidence repair-task filer is gone.

## Reopen criteria

Reopen this finding as a Slack repair only if **all** of the following
become true:

1. Independent evidence names a concrete Slack surface that still exists
   (module, CLI, notifier/communication binding, secret-fetcher path, or a
   failing test id), not merely the word `slack` in a task title.
2. That evidence includes a non-blank error signature, reproduction, or
   dated incident — not only a `deployment_learning:*` closure of an audit
   / dream-repair investigation.
3. Hub (or equivalent ledger) lookup of the cited memory id succeeds and
   shows a payload that is not the prior lineage task's own outcome
   snapshot.

Until then, treat `dreamrepair:4becfa8dfd2a06539d4dbb0fb4f53be9` as **not
actionable**. Residual risk is the still-reachable manual/API
`nap-consolidate` path (`emit_dream_artifacts=True` by default), which can
re-emit title-echo `dream:failure_pattern` rows, but it cannot mint
`dreamrepair:` fingerprints or file repair tasks in this tree.
