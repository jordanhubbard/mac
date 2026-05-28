---
id: mac-xqn6.7
status: deferred
deps: []
links: []
created: 2026-05-26T17:39:48Z
type: task
priority: 1
parent: mac-xqn6
mac-task-id: pending:mac-xqn6.7
---
# Remediate c26 fleet registration, toolchain, and remote topology

Clean up the current c26 deployment state after the registration hardening is available. Rocky currently has a passing local c26 environment but a hub-local remote; horde has the contract but lacks clang/qemu and cannot push to GitHub over HTTPS. Normalize the intended fleet, toolchain, and canonical remote before re-importing or re-dispatching c26 work.

## Acceptance Criteria

c26 has one intended canonical project registration per fleet; the selected hub passes contract, toolchain, make smoke, and Git read/write preflight; stale failed/reviewing tasks from bad imports are closed, blocked, or superseded with human-readable reasons; Beads and MAC agree on the remaining c26 work.
