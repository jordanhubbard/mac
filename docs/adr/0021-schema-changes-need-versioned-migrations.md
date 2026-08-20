# ADR 0021 - Schema changes need versioned migrations, not an append-only helper list

- Status: **Proposed**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0013 (one authoritative hub allocator), ADR 0020 (a running task
  is not editable — the change that surfaced this)

## Context

Adding the `stopped` task state (ADR 0020) required changing a Postgres trigger
function. The question "will a redeploy apply that?" turned out to have a
different answer depending on *what kind* of change it is, and nothing in the
system says which kind you have.

### What happens today

`PostgresStore.initialize()` re-reads and re-executes `schema.sql` on **every
control-plane startup** — `make_store_from_env()` defaults to
`initialize_schema=True` and the API uses that default. Its docstring is
explicit: *"Safe to call on an already-initialised database — every statement
uses `IF NOT EXISTS` or `OR REPLACE`."*

So a redeploy does apply some schema changes:

| Change | Applied by a redeploy? |
| --- | --- |
| New table | yes (`CREATE TABLE IF NOT EXISTS`) |
| New index | yes (`IF NOT EXISTS`) |
| Changed function, trigger, view | yes (`CREATE OR REPLACE`) |
| **New column on an existing table** | **no** |
| Type change, constraint, drop, rename, backfill | **no mechanism** |

`CREATE TABLE IF NOT EXISTS` is a no-op when the table exists, so a column
added to `schema.sql` never reaches a live database. The workaround is
hand-written: **29 `ensure_column()` calls** appended to `initialize()`, each
issuing `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. Fourteen of them are for
one table (`fleet_release_admission_episodes`), nine for `agents`, three for
`tasks`.

That list is the accumulated record of every column that could not be added
the normal way. It only grows.

### What this costs

**Only additive changes are possible at all.** There is no path to change a
column's type, add or drop a constraint, drop or rename a column, or backfill
data. A change of that kind today requires an operator connecting to the
database by hand, with no record that it happened.

**Nothing knows what has been applied.** There is no `schema_version` table, no
applied-migrations ledger, no ordering, and no down-migrations. "Is this
database up to date?" can only be answered by inspecting the live schema and
comparing it to the source by eye — which is exactly what had to be done to
answer the question that produced this ADR:

    select pg_get_functiondef(oid) from pg_proc
    where proname = 'trg_tasks_state_enum';

**Every startup takes DDL locks.** 29 `ALTER TABLE` probes run on each boot,
against live task and lease traffic. `make_store_from_env` already warns that
ancillary processes must pass `initialize_schema=False` precisely to avoid
"taking PostgreSQL DDL locks against live task and lease traffic" — the control
plane takes them by design, every time it starts.

**Drift is undetectable.** If a live database diverges from `schema.sql` in a
way `IF NOT EXISTS` cannot reconcile, nothing notices. The failure surfaces
later as a runtime error that reads like a caller bug. Adding `stopped` to
`mac.models.TaskState` without adding it to the trigger fails with
`invalid task state` — which looks like the code passing a bad value, not like
a database that is behind.

**The vocabulary is duplicated by hand.** `trg_tasks_state_enum()` hardcodes
the task states independently of `TaskState`. Nothing keeps them in step and
nothing detects the gap.

## Decision

### 1. Migrations are versioned, ordered, and recorded

A `schema_migrations` table records every migration applied, with its version,
a checksum of its content, when it was applied and by what process. Migrations
are ordered files, applied in order, exactly once.

The checksum matters as much as the version: it catches a migration edited
after it was applied somewhere, which is otherwise invisible and produces two
databases that claim the same version and are not the same.

### 2. `schema.sql` becomes a derived artifact, not the source of truth

Today `schema.sql` is both the bootstrap for a fresh database and, implicitly,
the intended state of an existing one — which is why the gap exists: it can
express the first perfectly and the second only when the statement happens to
be idempotent.

The migration sequence becomes the source of truth. `schema.sql` is generated
from it for fresh-database bootstrap, and a test asserts that applying every
migration in order produces exactly the generated `schema.sql`. That test is
what makes drift impossible rather than merely unlikely.

### 3. Non-additive changes become expressible

Type changes, constraints, drops, renames and data backfills are ordinary
migrations. They are also the dangerous ones, so a migration declares whether
it is safe to run against live traffic, and the deploy path refuses the unsafe
ones without an explicit operator acknowledgement.

### 4. Startup verifies rather than mutates

Control-plane startup checks that the database is at the expected version and
**refuses to serve** if it is behind, naming the missing migrations. It stops
issuing DDL on every boot.

This is a deliberate reversal. Today startup silently repairs what it can and
silently ignores what it cannot; the result is a hub that comes up against a
database it has not verified. Refusing loudly is better than serving on an
unknown schema.

Applying migrations becomes an explicit deploy step, which is also what makes
it possible to take a backup first.

### 5. The enum duplication is generated or asserted

`trg_tasks_state_enum()` must not be hand-maintained against
`mac.models.TaskState`. Either generate the trigger body from the enum, or add
a test that fails when they disagree. Generating is better; asserting is
acceptable. Leaving it to memory is what produced the runtime failure that
started this.

## Consequences

- A schema change stops being a question of "which kind is this, and will the
  redeploy notice?"
- Non-additive changes become possible without an operator editing production
  by hand and leaving no record.
- The deploy grows a step, and a hub can now refuse to start. That is the
  intended trade: a hub that will not serve on an unverified schema is safer
  than one that serves on whatever it finds.
- The 29 `ensure_column()` calls must be reconciled into the migration
  sequence, and existing databases baselined at the version they are already
  at. This is the fiddly part: the baseline must be derived from what is
  actually in the live database, not from what the source implies, or the
  first migration run tries to re-apply history.
- Postgres DDL is mostly transactional, so a failed migration should roll back
  cleanly — but `CREATE INDEX CONCURRENTLY` cannot run in a transaction and is
  exactly what a large table will need. That exception must be handled
  explicitly rather than discovered during an outage.

## Alternatives considered

**Keep `ensure_column` and add helpers for the other cases.** Rejected: it
grows a bespoke migration framework one primitive at a time, with no ordering,
no record, and no way to answer what has been applied. The 29 accumulated
calls are what that path looks like after a year.

**Adopt Alembic.** Not rejected — it is the obvious candidate and solves
versioning, ordering and autogeneration. Deferred only because the decision
above is about *what guarantees are required*; whether they come from Alembic
or a smaller in-repo runner is an implementation choice. Alembic's
autogeneration is also a poor fit for hand-written triggers and functions,
which this schema has several of.

**Do nothing; the additive case covers what we actually do.** Rejected on
evidence: it does not cover it. It failed for a trigger change during ADR 0020,
and the only reason that was caught is that a test suite ran against a fresh
database where `schema.sql` applies in full. Against the live hub the same
change would have failed at runtime, on the first `stop`, as `invalid task
state`.
