---
id: mac-fme
status: closed
deps: []
links: []
created: 2026-05-24T08:19:01Z
type: feature
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-fme
---
# Expose Hermes task lifecycle operation bridge

Hermes can now fetch MAC's work-context projection, but it still lacks a first-class Hermes-side operation surface for acting on MAC tasks and agents. Add adapter and CLI methods that let Hermes perform the durable task lifecycle operations it is allowed to perform through MAC's existing API, using the same task/project/agent objects from the work-context contract.

## Design

Use existing MAC API task lifecycle endpoints as the authority. Do not duplicate task state in Hermes; add Hermes-side convenience methods and CLI commands that operate on MAC task IDs, agent IDs, evidence IDs, review IDs, and publication targets.

## Acceptance Criteria

HermesMacAdapter exposes task lifecycle operations for get/detail, claim, start, transition, add evidence, submit for review, request review, and publish; mac-hermes CLI exposes matching commands; the work-context operation contract names these concrete adapter/CLI actions; tests prove calls use MAC API paths and payloads and update real ControlPlane state through the API transport.

## Notes

Continues the active goal: Hermes must align with MAC's first-class task/project/agent view and be able to operate through MAC like a direct CLI/API session.

## Close Reason

Added HermesMacAdapter task lifecycle operations and matching mac-hermes CLI commands for task detail, claim, start, transition, evidence, submit-review, request-review, claim-review, review-decision, and publish. Updated the work-context operation contract and dashboard/docs. Verified real API-backed lifecycle state changes and CLI path/payload coverage; full suite passed with 373 tests plus node --check.
