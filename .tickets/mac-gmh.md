---
id: mac-gmh
status: closed
deps: []
links: []
created: 2026-05-25T03:44:02Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-gmh
---
# Fix Hermes agent identity and memory regression after fleet redeploy

Agents report that they no longer know their own identities and attempt to proxy for one another after the TokenHub fleet redeploy. Investigate MAC/Hermes identity binding, gateway routing, SOUL/MEMORY preservation paths, and deploy initialization behavior; restore per-agent identity and memory loading, add regression tests, redeploy, and verify Rocky/Natasha/Bullwinkle each answer as themselves without proxying.

## Close Reason

Restored per-agent Hermes identity continuity, memory aliases, strict Slack mention routing, fixed TokenHub bind readiness during deploy, redeployed Rocky/Natasha/Bullwinkle, and verified healthy identities/services.
