---
id: mac-8r1
status: closed
deps: [mac-ng2]
links: []
created: 2026-05-20T04:49:44Z
type: task
priority: 0
mac-task-id: pending:mac-8r1
---
# Test: forgeable manifest demonstration — non-existent SHA must be rejected

Write a test that produces a syntactically-perfect but obviously fake mac-evidence.json — e.g., repo.head_sha = 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef' (a SHA shape that won't exist in any real remote) with pushed=true, well-formed repo + tests sections. Today such a manifest passes _assess_default_review_evidence. Once attestation (issue 2) lands the test should assert the same manifest is REJECTED because the signature is missing/invalid or the remote can't confirm the ref. Before the fix the test demonstrates the security model gap; after, it pins that the gap is closed.

## Close Reason

Closed
