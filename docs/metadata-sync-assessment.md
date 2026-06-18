# Metadata sync assessment (post-bd-bridge)

**Question:** now that MAC controls the full CLI interface and the
ticket contract (no more bd ↔ MAC round-trip), do we need to sync
repository metadata or fleet hub task metadata?

**Short answer:** No new sync work is required. The vestigial
beads-shaped fields on tasks and the `beads_repositories` table are
load-bearing for the existing registry/contract logic but no longer
need ongoing reconciliation across hosts.

## What "sync" used to mean

When bd was the parallel issue store:

- `bd dolt push/pull` shipped the embedded SQLite/dolt DB across hosts
- the bridge poll re-imported open beads issues and re-derived MAC
  tasks per agent heartbeat
- on claim/close, `_run_bd_for_task` wrote back to bd so the bd CLI
  saw the same state
- the hub kept a `beads_repositories` row that pointed at the on-disk
  `.beads/<project>` path on every host

That entire layer is off:

- dolt sync gated off (`MAC_BEADS_DOLT_SYNC_ENABLED`, commit 1835af0)
- bridge polling + bd CLI writeback gated off (`MAC_BEADS_BRIDGE_ENABLED`, commit 64283fb)
- `.beads/` directories removed from mac, c26 repos; nanolang queued (commit e70bead)
- `.tickets/<id>.md` mirrors are the new git-trackable artifact

## What still uses the `beads_repositories` table

The table is now repurposed as the project registry, not a sync
target. Live use:

- `register_beads_repository` / `get_beads_repository` (services.py)
  store a (name, path, source, project, required_capabilities,
  metadata) row per registered project. Used at task-creation time to
  auto-fill `task.metadata.origin.repository_path` and the embedded
  `repository_contract`.
- `_normalize_task_execution_contract` reads the row to attach the
  contract to new tasks.
- the publish path (`_publish_git_target_if_needed`) reads
  `repository_contract.canonical_remote_url` (mac-y7ha) to refuse a
  worktree pointed at the wrong remote.

None of these require ongoing sync. The row is set at registration
time and re-read on each task creation; the canonical contract lives
in `.mac/project.yaml` in the registered repo. A hub-restart re-reads
both.

**Recommendation:** keep the table. Rename in a future cleanup
("registered_repositories" would be more accurate now), but no urgent
sync work.

## What still lives in `task.metadata` from the bd era

Today's tasks still carry these vestigial fields, populated by
existing task-creation code paths:

- `metadata.origin.bead_id` — set when the task was imported from a
  beads issue. Read by `_run_bd_for_task` (gated off now) and by
  audit/observability surfaces.
- `metadata.acc_metadata.beads_id` — duplicate of the above; legacy
  ACC migration artifact.
- `metadata.acc_metadata.beads_sync_claim_on_claim` /
  `beads_sync_close_on_complete` — booleans that gated the writeback;
  no-ops with the bridge off.
- `metadata.acc_metadata.repo_beads_workflow` — feature flag enabling
  the bd-style ledger comments.

These are read-only with the bridge off. They are not actively kept
in sync across hosts (the hub is the source of truth). Newly-created
tasks via `mac task create` from the new CLI no longer set
`origin.bead_id` (it's only set when the task was bridged in from a
beads issue).

**Recommendation:** leave them in place for now. A follow-up
"task.metadata schema simplification" can drop `acc_metadata.*` and
`origin.bead_id` once the new CLI surface has run for a few weeks
without bridge-era code referencing them. Filing that as a separate
issue rather than blocking the migration on it.

## What the new system requires for cross-host parity

- `.tickets/<id>.md` files are checked into git. `git push` from any
  host propagates them. No extra sync.
- The MAC hub task ledger is host-local SQLite on the hub agent. All
  other agents talk to the hub via the control plane API, so there is
  no per-host ledger drift.
- `mac task migrate-beads <repo>` can re-emit `.tickets/<id>.md` from
  a stale `.beads/issues.jsonl` if a contributor still uses the bd
  CLI offline. Useful as a one-shot, not a sync.

## Open follow-ups (filed as separate issues)

- `mac-jfns` (P0): Hermes executor non-determinism (the actual
  autonomous-merge blocker; not a sync problem)
- `mac-ykkc` (P0): reviewer review-claim retry storm (also blocks
  autonomous merge; bridge-off doesn't fix it)
- `mac-zpku` (P0): mac-worker macOS push auth (resolved on inspection;
  deploy key is installed and `ssh -T git@github.com` works; v2
  failure was Hermes choosing not to push, not auth)
- Schema cleanup (not yet filed): drop `acc_metadata.beads_*` and
  `origin.bead_id` from newly-created tasks; rename
  `beads_repositories` table to `registered_repositories`. Cosmetic;
  low priority.

## TL;DR

No sync layer needs to be built. The hub is the authority for tasks;
`.tickets/<id>.md` is the git-distributed mirror; `beads_repositories`
is a registry that doesn't change at runtime. The remaining work is
cosmetic schema cleanup, not synchronization.
