---
id: mem-14
status: open
deps: []
links: [mem-01]
created: 2026-05-29T03:50:00Z
type: bug
priority: 2
assignee:
mac-task-id: task_d0db3d33cbf84c7bb9c192e283bf65f3
audit: memory-tier-2026-05-28
discovered_via: mem-01..mem-13
---
# mac CLI silently writes to local SQLite when invoked off-hub

**Discovered while filing the memory-tier audit tickets on 2026-05-29.**

The `mac` CLI (entry point `src/mac/cli.py`) is SQLite-direct: it opens
`mac.db` in `--db` or the cwd and writes to it. A separate CLI `hgmac`
(`src/mac/hgmac.py`) is the HTTP client that targets a hub. There is no
flag, env var, or auto-detection that makes `mac task create` go through
a hub.

Consequence: I (and any other human or agent) running `mac task create`
from the project checkout silently wrote 13 tickets to
`/Users/jordanh/Src/mac/mac.db` instead of the rocky hub. The fleet
never saw those tickets. The bug was invisible until called out.

`CLAUDE.md` says: *"The MAC hub task ledger (`mac task`) is the canonical
execution store"* — so the user-facing contract is "`mac task` writes to
the hub", but the implementation writes to local SQLite.

## Acceptance Criteria

Pick one of (a) / (b) / (c) and document the choice in an ADR:

- **(a) merge**: have `mac` route to the hub when `MAC_API_URL` (or a
  per-fleet `MAC_API_URL__<FLEET>` from `~/.mac/.env`) is set, falling
  back to SQLite only when neither is available *and* the user passed
  `--db` or `--local` explicitly. `hgmac` becomes a thin alias.
- **(b) error**: if `mac task <write-command>` is invoked without a
  reachable hub configured, refuse with a clear message pointing the
  user at `hgmac` or `--db`. Read-only commands may still hit the
  local SQLite for offline debugging.
- **(c) keep separate but warn**: every `mac task` write prints a
  banner on stderr identifying the database path being written.

Whichever path is chosen, the test must include: "running `mac task
create` with no MAC_API_URL set produces output that makes it obvious
where the row landed."

Backfill task: audit the `mac.db` files lying around developer machines
for orphan tickets; either move them to the hub or close them with a
pointer.

Discovered while filing [mem-01..mem-13] for the memory-tier audit.
The 13 orphan local tickets were moved to rocky and the local versions
cancelled in 37d4d9c → 2fbd370.
