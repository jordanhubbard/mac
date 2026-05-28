---
id: mac-y3w
status: closed
deps: []
links: []
created: 2026-05-23T20:21:57Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-y3w
---
# Enable loop mode on Rocky fleet worker agents

Natasha and Bullwinkle are configured in heartbeat mode, so they register and heartbeat but never consume review nudges or execute work. Switch the active Rocky fleet worker configuration to loop mode and redeploy so the agents actually process tasks and reviews.

## Close Reason

Natasha and Bullwinkle were switched to loop mode, redeployed against Rocky, and verified healthy. The hub URL loop-mode deployment bug and stale attestation-key drift were fixed; Natasha processed a review verdict and completed the lifecycle proof.
