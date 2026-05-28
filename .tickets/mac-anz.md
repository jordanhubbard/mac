---
id: mac-anz
status: closed
deps: []
links: []
created: 2026-05-20T04:45:13Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-anz
---
# Restore per-agent Hermes model diversity

Port ACC's per-agent model diversity into mac so Rocky, Natasha, and Bullwinkle can use different Hermes gateway/oneshot models through upstream Hermes and TokenHub instead of a fleet-wide model monoculture.

## Close Reason

Implemented per-agent Hermes model/runtime selection in mac deploy and startup shims, documented the fleet model split, and covered it with startup/deploy regression tests.
