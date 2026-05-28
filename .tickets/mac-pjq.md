---
id: mac-pjq
status: closed
deps: []
links: []
created: 2026-05-24T09:54:18Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-pjq
---
# Persist Hermes submitted runtime proof for dashboard

mac-hermes can submit the local Hermes startup report to the runtime-proof API, but the dashboard still recomputes runtime proofs from the hub startup report and does not retain the submitted agent-local proof. Persist the latest submitted runtime proof evidence on the Hermes instance and prefer it in dashboard state so UI proof reflects the actual Hermes agent runtime/prompt object model.

## Close Reason

Persisted submitted Hermes runtime proof evidence and dashboard now prefers the latest agent-submitted proof.
