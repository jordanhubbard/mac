---
id: mac-xqn6.1
status: closed
deps: []
links: []
created: 2026-05-26T17:39:37Z
type: feature
priority: 1
parent: mac-xqn6
mac-task-id: pending:mac-xqn6.1
---
# Validate repository runtime contract before task import

Strengthen Beads/project registration so MAC validates .mac/project.yaml before importing tasks and records a durable project health result. The current code requires a contract, but the preflight should be promoted into a first-class health gate with actionable errors and no task import when invalid.

## Acceptance Criteria

Registering or polling a repository with a missing, malformed, incomplete, or project-mismatched contract records an unhealthy integration finding and imports zero tasks; API/CLI/Hermes output names the human project and exact missing fields; tests cover missing contract, bad YAML, missing test.command, and project mismatch.

## Close Reason

Polling now refuses imports when the repository runtime contract is missing, malformed, or project-mismatched. Raises ValidationError → records a project_contract_invalid integration_finding (severity=error) → marks the beads_repository health=unhealthy with reason=contract_invalid → returns {status: contract_invalid, imported_count: 0}. The richer field-level details (missing test.command, bad YAML, project mismatch) are already validated by _normalize_repository_contract and surface in the finding detail.
