---
id: mac-coh
status: closed
deps: []
links: []
created: 2026-05-25T06:53:00Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-coh
---
# Prevent stale review verdict reuse across review attempts

Live c26 recovery showed default review can create a new review for an old executor evidence row and immediately consume an older rejected review_verdict from the same reviewer because verdict lookup only matches reviewer and reviewed_evidence_id. Require verdict evidence to be created during or after the review request and add regression coverage.

## Close Reason

Fixed stale review_verdict reuse by requiring default-review verdict evidence to be created at or after the review request; added regression coverage for repeated review attempts on the same executor evidence.
