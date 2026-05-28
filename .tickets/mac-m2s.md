---
id: mac-m2s
status: closed
deps: []
links: []
created: 2026-05-18T05:57:55Z
type: feature
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-m2s
---
# Add dashboard controlled actions

Add scoped write actions to the dashboard after the read-only views are stable.

## Design

Keep high-risk actions explicit and token-scoped; do not expose raw secret reveal as a casual dashboard action.

## Acceptance Criteria

Operators can run dispatch tick, transition tasks, request/submit reviews, and advance or pause rollouts from the UI with scoped API tokens and clear results.

## Close Reason

Added dashboard operator actions for dispatch tick, task claim/start/transition/evidence/review/publication, rollout advance/health/rescue, and secret handle requests with scoped API calls.
