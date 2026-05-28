---
id: mac-etu
status: closed
deps: []
links: []
created: 2026-05-22T06:49:05Z
type: task
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-etu
---
# Commit deploy Python and optional tooling fallback

Commit the existing deploy/deploy-mac-fleet.sh change that separates Hermes Python selection, allows the mac deploy path to run on Python 3.10, and makes Beads/GitHub CLI setup best-effort.

## Close Reason

Committed deploy Python/tooling fallback change. Verified bash -n and git diff --check; uv pytest target has pre-existing failures outside this diff.
