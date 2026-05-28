---
id: mac-lk3
status: closed
deps: []
links: []
created: 2026-05-23T19:38:25Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-lk3
---
# Prevent Beads side effects from blocking agent requests

Rocky workers time out while starting and heartbeating because synchronous Beads poll/writeback subprocesses run on API request paths. Move Beads side effects off hot agent endpoints and make heartbeat polling single-flight so task execution can proceed even when bd/dolt is slow.

## Close Reason

Fixed: Beads poll/writeback side effects are single-flight best-effort and agent hot endpoints now schedule them after returning.
