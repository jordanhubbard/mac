---
id: mac-vaze
status: closed
deps: []
links: []
created: 2026-05-27T00:53:56Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-vaze
---
# Re-register-with-new-signers is non-transactional and spoofable

src/mac/deploy_service.py:109-134 — existing-digest re-registration runs outside store.transaction(). Concurrent re-registrations read the same existing_signers and the later UPDATE wins, silently dropping signers. The API has no requirement that created_by actually possesses a signing key, so signer lists are spoofable strings. Fix: serialize in a transaction; bind signers to authenticated principals; require attestation evidence.

## Close Reason

Closed
