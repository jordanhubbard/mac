---
id: mac-wsny
status: in_progress
deps: []
links: []
created: 2026-05-28T06:14:56Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-wsny
---
# Design: replace beads with native MAC tickets (mac task / mac issue)

Strategic context: MAC always runs with a hub node, which is already the authoritative task ledger. Beads exists to be a distributed local-first issue store with cross-host dolt sync, but in this deployment:

- 9,708 bridge.beads.dolt_pull_failed events / 7 days; same error every time (`embeddeddolt: migrate: pending schema migrations alter pre-existing dirty tables: issues`)
- 4,731 rebuilds that just reconstruct from .beads/*.jsonl — proving JSONL files are the source of truth
- Cross-host beads sync never works empirically
- Dolt sync now disabled by default (commit 1835af0); beads CLI keeps working against local JSONL

If we drop beads entirely we get a single CLI (`mac task` / `mac issue`), one source of truth, and zero bridge surface. Inspiration: file-based ticket systems like https://github.com/wedow/ticket.git — one markdown file per ticket, git-tracked, no DB. We'd implement the same idea in Python with the hub task ledger as the canonical store and tasks/<id>.md as an optional git-trackable mirror.

Proposed scope of the design phase:

1. **CLI surface**: define `mac issue` (or extend `mac task`) to cover what beads provides today:
   - `mac issue create <title> --type=bug|feature|task --priority=N --description=...`
   - `mac issue ready` (issues with no blockers, not claimed)
   - `mac issue list --status=open|in_progress|closed`
   - `mac issue show <id>`
   - `mac issue update <id> --claim` / `--status=...`
   - `mac issue close <id> --reason="..."`
   - `mac issue dep add <id> <depends-on>` (dependencies)
   - Plus the human ergonomics: `mac issue blocked`, `mac issue search <kw>`, `mac issue stats`
2. **Memory**: confirm `mac memory add/search` covers `bd remember/memories` patterns; close the gap if not.
3. **File mirror format**: tasks/<id>.md with YAML front-matter (title, status, priority, type, dependencies) and a markdown body. Hub task is canonical; file is a regenerated mirror for git diff legibility.
4. **Session protocol**: replace `bd prime` with a `mac prime` (or remove the hook entirely and rely on `mac issue ready` + first-class CLI help).
5. **Migration**: one-time script to convert the 53 open beads issues to MAC tasks. Preserve the original beads ID as task metadata so historical references resolve.
6. **CLAUDE.md / AGENTS.md / hermes_runtime**: rewrite to use mac issue commands.
7. **Tear-out**: delete beads_bridge_service, _sync_beads_database, _rebuild_beads_database, _run_beads_dolt_pull, and related code paths. Remove the bd dependency from install.
8. **Back-compat**: keep `.beads/issues.jsonl` ingestion as a one-way archive importer for projects that arrive with existing beads state.

Deliverable for this design issue: a single follow-up implementation plan / set of issues, not code. Pre-design check: does anyone outside this fleet rely on the beads CLI? If yes, scope the replacement to coexist; if not, simplify by deleting outright.

What this is NOT: today's commit 1835af0 (disable dolt sync) is the tactical fix. This issue covers the strategic replacement.

## Acceptance Criteria

- A written design proposing the mac issue CLI surface, file-mirror format, memory mapping, and session protocol changes
- A migration plan for the 53 open beads issues (mac-l6o0, mac-8c0z, mac-ykkc, mac-73cz at minimum)
- An identified back-compat / coexistence plan (or explicit decision to drop)
- A set of follow-up implementation issues filed against this design, scoped small enough to ship one at a time
