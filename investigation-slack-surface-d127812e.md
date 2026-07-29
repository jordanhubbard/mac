# Slack Provider Surface Analysis: Dream Finding `dreamrepair:d127812e01d62630fa16797756580276`

Distinct-agent **surface-analysis** record for plan node `surface_analysis`,
parent audit task `task_b04d1726fa6e4486b8a5154d0037d356` (title: "Investigate
low-confidence dream finding: slack"). This complements the sibling `ground_truth`
record (`investigation-slack-finding-d127812e.md`) with an independent, read-only
examination of the actual slack provider integration surface — provider
configuration, notifier/messaging paths, and slack-related skills/tools — to
confirm or refute a reproducible code/config defect behind the finding. No `src/`,
`tests/`, `skills/`, or `deploy/` behaviour is changed; this note is the only
artifact.

## Summary Verdict

**No slack failure pattern is present in the code/config surface.** The finding
names zero affected skills, tools, or repo areas; the slack provider surface it
would implicate is present, coherent, and green. The provider config, notifier
messaging paths, gateway adapter, CLI, and slack skills/tools all import cleanly
and pass their targeted tests (107/107 in the focused slack suite; the sole
failure encountered in the broader run is an unrelated coding-agent CLI probe, not
slack). The `slack` label on this finding is an incidental keyword match, matching
the ground-truth verdict. **Disposition: refuted as a slack code/config defect.**

## 1. Scope And Method

Examined the three surface areas named in the task, read-only:

- **Provider configuration:** `src/mac/hermes_config_surface.py`,
  `src/mac/_hermes/gateway/config.py`, `src/mac/_hermes/gateway/platforms/slack.py`.
- **Notifier / messaging paths:** `src/mac/notifier_service.py`,
  `src/mac/communication_service.py`, `src/mac/_hermes/tools/send_message_tool.py`,
  `deploy/sync-hermes-home-channels.py`.
- **Slack skills / tools / CLI:** `src/mac/_hermes/hermes_cli/slack_cli.py`,
  `skills/setup-mac-fleet/SKILL.md`, and the slack-touching test corpus
  (~70 test files reference slack).

Verification used the project venv (`.venv/bin/python`, Python 3.12.7) to run the
slack-focused test subset and to import the slack platform adapter.

## 2. Provider Configuration Surface — Present And Coherent

- `src/mac/hermes_config_surface.py` promotes slack tokens from
  `~/.hermes/slack_accounts.json` into `config['env']`
  (`_promote_slack_accounts_tokens`, around line 915): it `setdefault`s
  `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`/`SLACK_USER_TOKEN` so an explicit env token
  wins and multi-workspace accounts still drive connections. `slack_home_channel_name`
  is a first-class config key (around line 79).
- `src/mac/_hermes/gateway/platforms/slack.py` is a full slack-bolt Socket-Mode
  adapter with a clean optional-dependency guard: `SLACK_AVAILABLE` is set from a
  `try/except ImportError` around `slack_bolt`/`slack_sdk`/`aiohttp`, so absence of
  the SDK degrades gracefully rather than crashing on import.
- Confirmed by import: with the SDK absent in this sandbox,
  `gateway.platforms.slack` imports successfully and reports
  `SLACK_AVAILABLE = False` — the graceful-degradation path works.

No missing key, misparsed token, or import-time fault was found.

## 3. Notifier / Messaging Paths — Present And Coherent

- `src/mac/notifier_service.py` treats slack as a first-class channel:
  `SUPPORTED_CHANNEL_TYPES = {"hermes", "slack", "telegram"}` (line 59), with
  slack-specific routing and openclaw-outbox handling
  (lines 310, 361, 387, 480, 500, 530).
- `src/mac/communication_service.py` lists `"slack"` among supported platforms
  (line 43).
- `src/mac/_hermes/tools/send_message_tool.py` has slack target parsing
  (`_SLACK_TARGET_RE`, `_SLACK_THREAD_TARGET_RE`) and DM resolution via
  `conversations.open` for `U...` user IDs (lines 29-31, 290-294).

These paths are exercised by `tests/test_notifier_service.py`,
`tests/test_communication_service.py`,
`tests/test_slack_thread_participant_triggers.py`, and
`tests/test_worker.py::test_mac_worker_forwards_notifier_status_updates_to_slack_home_channels`
— all passing.

## 4. Slack Skills / Tools / CLI — Present And Coherent

- `src/mac/_hermes/hermes_cli/slack_cli.py` implements `hermes slack manifest`,
  generating the slack app manifest from `COMMAND_REGISTRY` so slash commands stay
  in sync. No defect observed.
- `skills/setup-mac-fleet/SKILL.md` is the only `skills/` doc referencing slack
  (setup guidance); no slack skill logic is broken.
- Token-fetch and config-surface tests
  (`tests/test_slack_secrets_fetcher.py`,
  `tests/test_hermes_config_surface_slack_tokens.py`) pass.

## 5. Test Evidence (Reproduction Attempt)

Ran the slack-focused subset with the project venv:

- `tests/test_notifier_service.py`, `tests/test_communication_service.py`,
  `tests/test_slack_thread_participant_triggers.py`,
  `tests/test_slack_secrets_fetcher.py`,
  `tests/test_hermes_config_surface_slack_tokens.py`
  → **107 passed**.
- Broader messaging/worker/CLI/API slack-touching subset
  (`test_worker_communication.py`, `test_worker.py`,
  `tests/api/test_communication_api.py`, `tests/cli/test_cli_communication.py`,
  `test_sync_hermes_home_channels.py`, `test_gateway_display_channel_overrides.py`,
  `test_hermes_config_surface_approvals.py`)
  → **116 passed, 1 failed**.
- The single failure is
  `tests/test_worker.py::test_worker_falls_through_failed_claude_and_publishes_verified_codex`,
  which asserts a **coding-agent CLI verification** result
  (`clis["codex"]["verification_status"] == "verified"` got `unverified`). It is
  driven by claude/codex CLI probing in the sandbox and has **no slack code path**;
  the slack test in the same file
  (`test_mac_worker_forwards_notifier_status_updates_to_slack_home_channels`)
  passes in isolation. This is an environmental/CLI-probe artifact, not a slack
  defect, and is out of scope for this finding.

No reproducible slack error signature, stack trace, or failing slack test was
produced.

## 6. Cross-Check With Ground Truth

Consistent with `investigation-slack-finding-d127812e.md`: the finding is a
low-confidence (0.35, `evidence_count=1`) `failure_pattern` whose single evidence
record is a prior investigation task's own environment/executor-startup failure
closure memory (`mem_c052b78e...`, `error_signature=""`, only `returncode=1`). The
`slack` provider label comes from a `\bslack\b` word-boundary match on that prior
task's title, not from any diagnosed slack fault. This surface analysis
independently confirms there is no code/config defect for that label to attach to.

## 7. Verdict, Disposition, Reopen Criteria

- **Failure pattern present / absent / unverifiable:** **Absent** in the slack
  code/config surface. The provider configuration, notifier/messaging paths, and
  slack skills/tools are present, coherent, and green.
- **Disposition:** Refuted as a slack code/config defect; NOT ACTIONABLE as a
  slack repair. No `src/`, `tests/`, `skills/`, or `deploy/` change is warranted
  for the slack surface.
- **Note on the one-off/environmental angle:** the finding traces to a prior
  environment/executor-startup failure re-derived by the dream cycle, i.e. a
  pipeline/process artifact, not a slack fault. Any actionable remediation belongs
  to the dream-repair pipeline and is out of scope here.
- **Reopen criteria (slack track only):** reopen only if the slack provider
  acquires a reproducible failure signature — a real error signature, stack trace,
  or failing slack test naming a slack skill/tool/code path — backed by at least
  two independent, non-self-referential evidence records.

## 8. Verification Performed

- Read the provider config surface (`hermes_config_surface.py`,
  `gateway/platforms/slack.py`, `gateway/config.py`) and confirmed token promotion
  and graceful SDK-optional import.
- Read the notifier/messaging paths (`notifier_service.py`,
  `communication_service.py`, `send_message_tool.py`) and confirmed slack is a
  first-class, coherently-routed channel.
- Read the slack CLI (`slack_cli.py`) and the one slack-referencing skill
  (`skills/setup-mac-fleet/SKILL.md`); no defect found.
- Ran the slack-focused test subset (107 passed) and a broader slack-touching
  subset (116 passed, 1 unrelated coding-agent-CLI failure); verified the lone
  slack test in `test_worker.py` passes in isolation.
- Imported `gateway.platforms.slack` under the project venv: import OK,
  `SLACK_AVAILABLE=False`, confirming graceful degradation.
- Fleet-generic: no secrets, hostnames, personal paths, or operator identities are
  recorded.
