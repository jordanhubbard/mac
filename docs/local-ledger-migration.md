# Local Ledger Authority Transfer

`~/.mac/mac.db` is a complete SQLite control-plane authority, not an offline
hub replica. MAC does not merge it with a fleet hub. When an operator client
has active tasks in that database, transfer them explicitly and retire the
local authority with `mac migrate local-ledger`.

## Inspect first

The command is read-only unless `--execute` is supplied:

```bash
mac --json migrate local-ledger
```

The plan reports every active task, its current state, dependencies, satisfied
terminal dependencies, migration order, validation issues, and the exact source
database. Missing dependencies, dependencies ending in `failed` or `cancelled`,
invalid task JSON, and dependency cycles block execution. Completed, failed,
and cancelled tasks remain historical records in the eventual archive; they are
not recreated on the hub.

For a non-default source file:

```bash
mac --json migrate local-ledger --source-db /path/to/mac.db
```

## Select the hub and execute

Establish a scoped client profile, then select that remote authority. Do not use
global `--db`; `--source-db` identifies the source while `--profile`, `--fleet`,
or `--hub-url` identifies the target.

```bash
mac login --fleet default
mac --profile default --json migrate local-ledger --execute
```

Execution is a one-way authority transfer:

1. Re-read and validate the local task graph.
2. Discover prior hub copies through `mac.local_ledger_task_migration.v1`
   provenance so retries do not duplicate tasks.
3. Create active tasks in dependency order. Local leases, owners, repository
   paths, execution contracts, and repository-ref lifecycle state are not
   copied; the hub derives its own repository contract. Holds such as
   `metadata.no_dispatch` are preserved.
4. Read every hub task back and verify its title, project, priority,
   capabilities, dependency mapping, retry limit, and migration provenance.
5. Re-check that the local task set and timestamps have not changed.
6. Write a recovery database, cancel the local tasks as superseded by their hub
   replacements, and record durable cancellation history.
7. Create and integrity-check a SQLite archive, verify that it contains the
   cancelled records, hash it, write and read back a mode-`0600` manifest, and
   only then remove the live source path and mark the manifest completed.

The default archive directory is `~/.mac/archive`. The result reports both the
database archive and its JSON manifest. If cancellation or archive creation
fails, the recovery copy restores the original active database; the verified
hub copies remain safe for an idempotent retry.

## Login and diagnostics notice

`mac login`, `mac login status`, and `mac diagnostics` inspect the default local
ledger without modifying it. When active local tasks exist, their output
includes `local_ledger` or `client_local_ledger` with the task count, issues,
and the migration command.

Repository `.tickets/` files and GitHub issues are not part of this transfer.
They are compatibility or external planning records, not MAC execution state.
