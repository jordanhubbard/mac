---
id: mac-idl
status: closed
deps: []
links: []
created: 2026-05-23T22:13:33Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-idl
---
# Beads-imported repository tasks lack git publication target

The end-to-end bead lifecycle needs a reviewed repository bead to publish/merge into main. Imported Beads tasks currently carry repository origin metadata but no publication_target, so the default review workflow can stop after approval with waiting_for_publication_target.

## Acceptance Criteria

Repository tasks imported from Beads get a git://main publication target by default, and tests assert the imported task metadata includes that target.

## Close Reason

Beads-imported repository tasks now default to publication_target=git://main; regression asserts imported task metadata includes the target. Full suite: 368 passed.
