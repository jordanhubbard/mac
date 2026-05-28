---
id: mac-s0h
status: closed
deps: []
links: []
created: 2026-05-18T05:57:53Z
type: task
priority: 3
assignee: Jordan Hubbard
mac-task-id: pending:mac-s0h
---
# Add secrets and audit dashboard surface

Add a read-first dashboard view for secret handles, access audits, and operational provenance without exposing secret values.

## Design

Use the existing redacted secret APIs and secret audit rows; keep reveal flows separate and explicit.

## Acceptance Criteria

Dashboard shows redacted secrets, audit records, access outcomes, and provenance links while never rendering secret values.

## Close Reason

Added Secrets dashboard view with redacted secret records, handle request form, and audit records; no casual secret reveal action is exposed.
