---
id: mac-6sy
status: closed
deps: []
links: []
created: 2026-05-18T20:43:28Z
type: task
priority: 2
mac-task-id: pending:mac-6sy
---
# Audit inherited Hermes gateway warning policy

The rocky/natasha/bullwinkle mac replacement deploy succeeded, but Hermes gateway logs still expose inherited upstream/runtime warnings that should be policy decisions rather than silent background noise. Natasha and Bullwinkle inherit HERMES_REDACT_SECRETS=false from existing Hermes state, and Rocky/Natasha/Bullwinkle can emit Slack file_public unhandled-event warnings. Services remain active; decide whether mac should override redaction, document preservation of existing Hermes config, or add startup/report surfacing for these warnings.

## Acceptance Criteria

A policy decision is documented; mac deploy/startup either enforces or reports the chosen redaction behavior; non-actionable Slack event warnings are either filtered, handled, or documented as benign.

## Notes

Resolved as part of mac-lii. HERMES_REDACT_SECRETS=false was confirmed as drift, not intentional config. Deployment now rewrites inherited Hermes config/env redaction false values to true with backups, mac.env and wrappers force HERMES_REDACT_SECRETS=true, /startup/hermes reports redaction drift as readiness degradation, and gateway logs are classified for secret-redaction-disabled/actionable warnings versus benign controlled restarts and Slack file_public events. Final fleet verification reports redaction=True and warnings=0 on rocky, natasha, and bullwinkle.

## Close Reason

redaction policy enforced and gateway warning classifications implemented
