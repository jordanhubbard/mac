---
id: mac-buo
status: closed
deps: []
links: []
created: 2026-05-18T05:53:15Z
type: task
priority: 2
mac-task-id: pending:mac-buo
---
# Artifact registry (separate from per-publication content_hash)

## Close Reason

artifacts table (id, kind, digest UNIQUE, uri, sbom_uri, signers, metadata). Service: register_artifact (idempotent on digest, merges signers/metadata), get_artifact (by id or digest), list_artifacts (filter by kind). API /artifacts and CLI 'mac artifact register|list|show'. Tests cover augment-on-reregister, validation, kind filtering.
