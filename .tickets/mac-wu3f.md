---
id: mac-wu3f
status: closed
deps: []
links: []
created: 2026-05-27T00:53:18Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-wu3f
---
# Evidence signature MAC excludes signed_by — signature replay

src/mac/services.py:443 — _canonicalize_for_signature does not include signed_by in the MAC input. An attacker who captured a valid signature could replay it inside a different manifest with a different signed_by value; verification keys off the new signed_by, so the swap is undetectable when both agents share or reuse a key, and provides no defense-in-depth in general. Fix: bind signed_by into the signed payload.

## Close Reason

Closed
