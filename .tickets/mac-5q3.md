---
id: mac-5q3
status: closed
deps: []
links: []
created: 2026-05-23T18:45:37Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-5q3
---
# Fix Beads bridge recursive health serialization

The deployed Rocky Beads poll endpoint still returns HTTP 500 when repository health metadata contains prior authority drift details. The bridge must avoid embedding full repository metadata inside findings/health details so repeated polls remain serializable.

## Close Reason

Beads findings and imported payloads now use a shallow repository ref, regression covers repeated API polls with deeply nested prior health metadata, and full suite passes.
