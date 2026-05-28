---
id: mac-dyk
status: closed
deps: []
links: []
created: 2026-05-20T04:48:37Z
type: bug
priority: 0
mac-task-id: pending:mac-dyk
---
# Auto-review: cross-tenant leak in advance_default_review_workflows

services.py:1646 advance_default_review_workflows iterates cp.list_tasks() with no tenant filter, and _select_default_reviewer picks from cp.list_agents() with no tenant filter either. After we introduced tenant-bound bearer tokens (an alpha-writer can't cross to beta), a single /reviews/default/tick call lets tenant A's task be reviewed by tenant B's idle agent. In a fully-autonomous swarm the tenancy boundary IS the safety boundary — there is no human to notice the misroute. Fix: scope tasks by task.metadata.origin.tenant_id and require the reviewer's hermes_instance_id -> persona.tenant_id to match. Until this lands every tenant in the deployment effectively trusts every other tenant's agents to approve their work.

## Close Reason

Closed
