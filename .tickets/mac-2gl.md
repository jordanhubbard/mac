---
id: mac-2gl
status: closed
deps: []
links: []
created: 2026-05-22T16:53:56Z
type: epic
priority: 2
assignee: agent_natasha
mac-task-id: pending:mac-2gl
---
# Create hgmac CLI for complete agent operations

Build a dedicated hgmac command-line interface that provides complete, scriptable CRUD coverage for MAC agent operations. The CLI should cover agent lifecycle and configuration workflows currently exposed through the control plane, fill API gaps where CRUD operations are missing, and provide stable JSON output for automation.

## Acceptance Criteria

1. hgmac is installed as a console script and can target a MAC API server via explicit URL/token flags and config/env defaults.\n2. hgmac provides create, list, show, update, and delete/disable semantics for agent records where supported by the domain model, plus related agent operations such as heartbeat/status, capabilities/resources, role assignment, mood, nap schedule, command audit lookup, and safe registration workflows.\n3. Missing API endpoints required for full CRUD are added with authorization, validation, audit/history, and tests.\n4. Commands support non-interactive usage, JSON output, useful error messages, and exit codes suitable for automation.\n5. Documentation and tests cover primary agent workflows and backwards compatibility with the existing mac CLI is preserved.

## Close Reason

Implemented in this session: deploy hygiene/new-hub/reconcile/offline handling, service boundaries, typed validators/workflows, workflow drafts/preview/dashboard, generalized notifier delivery/configuration, and hgmac agent CRUD CLI; verified with full test suite.
