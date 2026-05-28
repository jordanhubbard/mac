---
id: mac-lzz
status: closed
deps: []
links: []
created: 2026-05-23T20:09:48Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-lzz
---
# Prevent review nudge floods from starving reviewer work

The default review workflow emits repeated produce_review_verdict nudges for the same pending review on each tick. Review workers deliver queued messages oldest-first and process one review nudge per run, so stale duplicate nudges can starve newer reviews and keep tasks stuck in reviewing. Deduplicate or reuse pending nudges so a reviewer sees one actionable review request per review, and add regression coverage.

## Close Reason

Fixed review verdict nudge dedupe and stale nudge handling; regression tests and full suite pass.
