---
id: mac-3a6
status: closed
deps: []
links: []
created: 2026-05-21T06:07:40Z
type: bug
priority: 0
mac-task-id: pending:mac-3a6
---
# Restore registered Beads export noise during hub polling

Production hub polling can observe .beads/issues.jsonl or .beads/config.yaml dirt in the registered runtime checkout even though polling uses a dedicated bridge checkout. When MAC_BEADS_RESTORE_TRACKED_EXPORTS=1, source refresh should restore tracked Beads export noise in the registered checkout before recording dirty source state so agent main branches do not remain dirty.

## Close Reason

Restored registered checkout Beads export noise during hub source refresh when MAC_BEADS_RESTORE_TRACKED_EXPORTS=1, including staged issues.jsonl-only cases, with regression coverage.
