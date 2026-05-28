---
id: mac-s2vz
status: closed
deps: []
links: []
created: 2026-05-27T00:53:28Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-s2vz
---
# Attestation key rotation silently breaks in-flight pending verdicts

src/mac/services.py:5211-5232 — rotate_agent_attestation_key re-encrypts a new key with no version tag; previously-signed pending verdicts then fail at publish time with a generic 'signature does not verify' message and no recovery hint. Fix: tag signatures with key version; keep retired keys for a verification grace window; produce a clear error for rotated-out signatures.

## Close Reason

Closed
