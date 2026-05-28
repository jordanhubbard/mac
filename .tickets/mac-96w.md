---
id: mac-96w
status: closed
deps: []
links: []
created: 2026-05-25T05:23:18Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-96w
---
# Fix fleet identity regression after TokenHub deploy

Agents on the Rocky fleet no longer preserve per-agent identity/memory boundaries after recent deployment changes. They appear to lose their own Hermes identity and attempt to proxy for one another. Diagnose deployed env/runtime context, fix bootstrap/runtime identity generation, add regression coverage, redeploy, and verify Rocky/Natasha/Bullwinkle each report their own soul, memory scope, Hermes instance, and agent id without stale cross-agent values.

## Close Reason

Fixed identity/bootstrap regression: Hermes-visible identity env is preserved, TokenHub credential pool is synced, startup self-test now validates identity/context/TokenHub/live Hermes chat, redeployed rocky/natasha/bullwinkle and verified all startup self-tests pass.
