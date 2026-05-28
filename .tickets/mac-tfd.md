---
id: mac-tfd
status: closed
deps: []
links: []
created: 2026-05-24T10:05:53Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-tfd
---
# Expose Hermes command audit bridge

MAC workers and hgmac can record and inspect command audit entries tied to agent/task execution, but the Hermes-facing mac-hermes adapter does not expose command-audit operations. Add Hermes adapter/API client methods and CLI commands to record and list command audit records, then include them in the MAC/Hermes operation, runtime, and proof contracts so Hermes agents have direct-session parity for auditable shell work.

## Close Reason

Added Hermes adapter and CLI command-audit record/list operations, wired command_audit into runtime/proof contracts, and covered the bridge with tests.
