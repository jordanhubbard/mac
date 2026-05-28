---
id: mac-jqb
status: closed
deps: []
links: []
created: 2026-05-20T04:48:26Z
type: bug
priority: 0
mac-task-id: pending:mac-jqb
---
# Auto-review: reviewer agent never actually reviews

In services.py:2295 advance_default_review_workflow picks a reviewer agent then in the same Python process checks _assess_default_review_evidence (which only inspects manifest shape) and calls submit_review(approved). The named reviewer agent does NO work: it never claims a review task, never fetches the remote ref, never re-runs tests, never produces its own evidence row. The second-eyes role exists in the audit trail but not in computation. Fix: workflow should create a review task for the reviewer agent (capability=review, role=code-reviewer when adopted), let it claim and execute independently, and submit its own evidence (re-run results / signed attestation) before submit_review accepts. The current path is a sticker, not a gate.

## Close Reason

mac-jqb: implemented in commit 41af31e. The default-review workflow now waits for a signed verdict evidence row authored by the reviewer agent (review_verdict manifest signed with the reviewer's attestation key) before publishing. mac-din: test_control_plane.py + test_e2e.py + test_provisioning_service.py now assert the reviewer-produced evidence row exists, is signed by reviewer.id, and is distinct from executor evidence.
