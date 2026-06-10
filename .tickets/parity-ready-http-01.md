---
id: parity-ready-http-01
status: open
deps: []
links: [parity-tickets-autoemit-01]
created: 2026-06-09T00:00:00Z
type: feature
priority: 2
audit: mac-task-bd-parity
discovered_via: parity_audit
---
# Serve `mac task ready` / `search` / `stats` over the hub (not SQLite-only)

## Why this exists

`bd ready --json` was the canonical ready queue and worked against the store
from anywhere. The `mac task` equivalents — `ready`, `search`, `stats` — reach
into `ControlPlane.store` for direct SQL, so in hub mode they're refused
(`src/mac/dispatch.py` `_RemoteStore._refuse`: "needs direct SQLite access").
An operator pointed at the rocky hub (the common case now that flagless `mac`
resolves the default fleet) can't run them at all. This is the biggest live
`bd`→`mac task` parity gap (see `docs/mac-task-bd-parity-audit.md`).

## Acceptance criteria

- `GET /tasks/ready` (open + dependencies satisfied + unclaimed, honoring
  `--project`/`--all` and `--limit`), `GET /tasks/search?q=...`, and a stats
  endpoint, all served by the hub.
- `mac task ready/search/stats` use those endpoints in hub mode instead of
  refusing; `--db` mode keeps the direct-SQL path.
- The ready endpoint reuses the dispatcher's dependency-satisfied semantics so
  CLI and dispatcher never diverge.

## Notes

Cross-fleet `stats` may need aggregation; scope can start with ready+search.
Related: [[parity-tickets-autoemit-01]].
