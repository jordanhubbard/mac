---
id: mac-2rb
status: closed
deps: []
links: []
created: 2026-05-20T08:22:37Z
type: task
priority: 0
mac-task-id: pending:mac-2rb
---
# Enable review-capable workers and reconcile Rocky checkout

Make review capability part of mac fleet worker deployment, reconcile Rocky's dirty Beads state so it can fast-forward to the repository contract commit, and verify live agents can advance reviewable work.

## Notes

Implemented review-capable worker defaults, review verdict nudge handling, and attestation key recovery. Local full pytest: 270 passed.

## Close Reason

Implemented and deployed review-capable worker defaults, autonomous signed review-verdict nudge handling, attestation-key persistence/recovery, deploy drain fixes, host-local bd discovery, and production Beads export cleanup. Verified local pytest: 272 passed. Rolled out to rocky/natasha/bullwinkle at 68593b8; all agents healthy with review capability and clean source checkouts.
