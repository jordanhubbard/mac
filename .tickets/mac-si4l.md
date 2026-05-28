---
id: mac-si4l
status: closed
deps: []
links: []
created: 2026-05-27T00:53:23Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-si4l
---
# /agentbus/repo-update has no authority check — unauthenticated fleet-wide restart

src/mac/api.py:3190-3219 — any agent-scoped token can POST /agentbus/repo-update with restart=true and all_agents=True. No signature, no sender allowlist, no verification that the named branch matches what mac itself tracks. This is a one-call full-fleet restart primitive. Fix: require admin scope, sender allowlist, or signed update; also require principal binding (see #5).

## Close Reason

Closed
