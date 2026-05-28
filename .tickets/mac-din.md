---
id: mac-din
status: closed
deps: [mac-jqb, mac-ng2]
links: []
created: 2026-05-20T04:49:16Z
type: task
priority: 0
mac-task-id: pending:mac-din
---
# Test: pin that reviewer agent performed independent work (audit trail of review)

Current tests for the default review workflow (test_control_plane.py:534+) assert result.status == 'published' and that a Review row exists with status='approved'. None assert the reviewer did ANY actual work: no evidence row from the reviewer, no claim of a review task, no re-run results. Once issues 1+2 land (reviewer actually reviews and produces attested evidence), pin the property that the audit trail of a review is non-empty: at least one evidence row authored by the reviewer agent_id, distinct from the executor's evidence_id. This test is the regression guard against silently regressing back to sticker-review.

## Close Reason

mac-jqb: implemented in commit 41af31e. The default-review workflow now waits for a signed verdict evidence row authored by the reviewer agent (review_verdict manifest signed with the reviewer's attestation key) before publishing. mac-din: test_control_plane.py + test_e2e.py + test_provisioning_service.py now assert the reviewer-produced evidence row exists, is signed by reviewer.id, and is distinct from executor evidence.
