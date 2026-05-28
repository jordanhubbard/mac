---
id: mac-xqn6.4
status: deferred
deps: []
links: []
created: 2026-05-26T17:39:43Z
type: feature
priority: 1
parent: mac-xqn6
mac-task-id: pending:mac-xqn6.4
---
# Verify canonical Git remote read/write access at project registration

Before repository-backed work is imported, MAC must prove that the registering hub/agent can read and publish to the canonical remote. c26 on horde could read GitHub but failed non-interactive HTTPS push; Rocky could push only to a hub-local bare repo, which does not prove GitHub publication access.

## Acceptance Criteria

Registration records canonical remote URL and publication target; MAC runs non-interactive read and dry-run write probes; HTTPS credentials, SSH host keys, and deploy-key failures produce actionable findings; repositories without write access remain unhealthy and import/dispatch no publication-required tasks; tests cover read-only and no-credential remotes.
