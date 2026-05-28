---
id: mac-r33
status: closed
deps: []
links: []
created: 2026-05-22T17:14:40Z
type: feature
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-r33
---
# Make dashboard URL-addressable and closer to single-pane operations

Improve the MAC UI dashboard so operators can deep-link and share specific views, filters, and selections, and extend the dashboard toward a real single pane of glass with better relationship views rather than only card/list presentations.

## Acceptance Criteria

1. Dashboard view and user-selectable filters/selections are reflected in the URL and restored on load/back/forward.\n2. URLs can be shared/bookmarked to open specific dashboard views, filtered task/agent results, and selected records.\n3. Additional control-plane domains that are currently present in dashboard state but not rendered are surfaced in the UI.\n4. Relationship-heavy domains use purpose-built visualizations where helpful, such as dispatch/task dependency, workflow, runtime rollout, or agent topology graphs.\n5. UI remains responsive and professional on desktop and mobile.

## Close Reason

Implemented URL-backed dashboard views/filters/selection, added map/operations/integrations single-pane coverage, surfaced additional dashboard state domains, and added SVG relationship visualization plus API/UI tests.
