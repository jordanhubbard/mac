---
id: mac-er6u
status: closed
deps: []
links: []
created: 2026-05-27T00:53:22Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-er6u
---
# Publication content_hash = evidence.checksum (worker-supplied opaque string)

src/mac/review_service.py:237, src/mac/services.py:4503-4533 — add_evidence accepts checksum as an opaque caller-supplied string with no format validation, no recomputation, no link to actual artifact bytes. publish_task uses this string verbatim as the publication content_hash. 'Tamper-evidence' reduces to whatever string the worker chose to write. Fix: server-side recompute the hash from the artifact bytes / URI, or require digest format + cross-checks.

## Close Reason

Closed
