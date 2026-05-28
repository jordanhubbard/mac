---
id: mac-fys
status: closed
deps: []
links: []
created: 2026-05-24T08:06:25Z
type: feature
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-fys
---
# Expose MAC task/project context contract to Hermes

Hermes needs a first-class, MAC-authoritative view of projects, tasks, agents, and available task operations so a Hermes session can reason about MAC work like a Claude or Codex session working directly in this repo. Add a concrete context bridge slice across MAC API, CLI, UI, and the Hermes adapter instead of leaving Hermes to infer from separate endpoints.

## Design

Create a MAC-authoritative Hermes work context projection rather than duplicating state in Hermes. The projection should be derived from existing MAC durable records and project summaries, include stable operation hints for API/CLI use, and remain safe for human-facing Hermes runtimes.

## Acceptance Criteria

API exposes a Hermes-facing task/project context payload for a Hermes instance; the Hermes adapter and mac-hermes CLI can fetch and render it; the dashboard surfaces the context bridge on the Hermes view; tests prove the payload contains projects, tasks, agents, allowed operations, and project/task relationships from MAC's current state.

## Notes

Part of active goal: tasks and projects must be first-class citizens in both MAC and hermes-agent through a coupling mechanism aligned across API, CLI, UI, and Hermes understanding.

## Close Reason

Implemented MAC-authoritative Hermes work-context bridge: API endpoint, ControlPlane projection, mac CLI, mac-hermes adapter/CLI, dashboard surfacing, docs, and tests proving projects/tasks/agents/operations/relationships come from MAC state. Verified with 370 passing contract tests and node --check for dashboard JS.
