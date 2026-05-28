---
id: mac-lii
status: closed
deps: []
links: []
created: 2026-05-18T21:00:54Z
type: task
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-lii
---
# Harden mac replacement deployment from transition findings

Implement the post-transition improvements observed during the rocky/natasha/bullwinkle ACC-to-mac replacement: deterministic Hermes dependency installation, enforced secret redaction, upstream-compatible Hermes service semantics, richer idempotent deploy manifests/logs, explicit no-ACC-DB host classification, operator-facing Hermes state health, benign Slack warning classification, and rollback/rescue behavior.

## Acceptance Criteria

deploy/deploy-mac-fleet.sh preinstalls Hermes dependencies before service start; HERMES_REDACT_SECRETS=false is corrected or blocked as drift; mac-managed Hermes systemd semantics match upstream restart/drain behavior; each deploy writes before/after manifests; no-ACC-DB hosts are classified explicitly; /startup/hermes surfaces state/security/log classifications; rollback artifacts and a rollback command are documented; tests pass and fleet redeploy verifies all changes.

## Notes

Implemented transition hardening from the rocky/natasha/bullwinkle cutover. deploy/deploy-mac-fleet.sh now backs up source/venv/Hermes/service artifacts, writes rollback-latest.sh and structured pre/post manifests, recreates mac venvs deterministically, reclones upstream Hermes and applies the multi-Slack patch, preinstalls configured Hermes lazy messaging deps, disables runtime lazy installs, normalizes inherited secret-redaction=false drift in Hermes config/env files, classifies no-ACC-DB hosts, classifies Hermes gateway logs, and installs upstream-compatible Hermes gateway systemd restart semantics. /startup/hermes now reports security/log/operator health and refreshes on request; dashboard state includes it. Final fleet verification: rocky, natasha, bullwinkle all ready=True, warnings=0, redaction=True, operator_status=healthy, log actionable=0; rocky migration already_imported/acc_migrated with 143 tasks, natasha and bullwinkle no_acc_sqlite_db/hermes_state_only.

## Close Reason

implemented transition hardening and verified on rocky, natasha, and bullwinkle
