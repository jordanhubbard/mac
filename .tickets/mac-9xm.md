---
id: mac-9xm
status: closed
deps: []
links: []
created: 2026-05-20T02:40:00Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-9xm
---
# Require verifiable evidence before default auto-publication

The default review workflow auto-approves work from returncode=0 and a log evidence row. That let thin or local-only artifacts be published as completed. Require typed, verifiable evidence for auto-review/publication: repo work must prove reachable git artifacts or explicit non-code/deploy evidence, and unverifiable evidence must remain in needs_review/reviewing for manual handling.

## Close Reason

fixed
