---
id: mac-0a8o
status: closed
deps: []
links: []
created: 2026-05-27T00:53:35Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-0a8o
---
# Artifact hash is never recomputed against URI content (verification theater)

src/mac/rollout_service.py:516-521 — _validate_artifact_hash only checks the sha256: prefix and digest length >= 6. The 'rollout.artifact_verified' event at lines 151-156 and 224-229 is theatrical: no fetch of artifact_uri, no digest recomputation, no cross-check against the artifacts table. src/mac/deploy_service.py:88-156 register_artifact likewise trusts caller-supplied digest+uri without fetching/hashing. Fix: actually fetch and digest the artifact URI before claiming verification.

## Close Reason

Closed
