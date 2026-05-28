---
id: mac-2pp
status: closed
deps: []
links: []
created: 2026-05-20T16:06:44Z
type: feature
priority: 1
assignee: agent_natasha
mac-task-id: pending:mac-2pp
---
# Add agentic workflow planning drafts and upfront questions

The desired workflow creation path is not just raw YAML/JSON editing. A human should be able to describe a goal, have an agent propose a multi-step plan and all clarifying questions up front, answer those questions once, edit the plan, and then convert it into executable tasks.

## Acceptance Criteria

A persisted workflow draft model/API stores the initial goal, proposed steps, agent questions, human answers, and edit history; the UI walks the human through goal entry, question answering, step editing/reordering, and approval; approved drafts compile into workflow definitions and task runs without losing the answered context.

## Notes

mac task task_312353b96d0744d68e2053e3b92eb4a9 failed: beads_failed_task_retry_limit
mac task task_312353b96d0744d68e2053e3b92eb4a9 failed: beads_failed_task_retry_limit
mac task task_312353b96d0744d68e2053e3b92eb4a9 failed: beads_failed_task_retry_limit
mac task task_312353b96d0744d68e2053e3b92eb4a9 failed: beads_failed_task_retry_limit
mac task task_312353b96d0744d68e2053e3b92eb4a9 failed: beads_failed_task_retry_limit

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
