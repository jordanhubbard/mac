---
id: mac-s1a
status: closed
deps: []
links: []
created: 2026-05-20T04:48:44Z
type: bug
priority: 0
mac-task-id: pending:mac-s1a
---
# Auto-review: reviewer selection pool too wide; require review capability + role match

services.py:2470 _select_default_reviewer accepts any healthy non-owner agent with a 'prefer review capability' tiebreak. An autonomous review should require the capability, not prefer it. Refuse to advance when no agent has 'review' capability. Better still: gate on a specific role slug derived from manifest.evidence_type — code-reviewer for repo_change, qa-engineer for test, devops-engineer for deployment. The evidence-type taxonomy already exists in the manifest; wire it to role selection so the agent doing the review is qualified by role, not just any idle worker that happens to be online.

## Close Reason

Closed
