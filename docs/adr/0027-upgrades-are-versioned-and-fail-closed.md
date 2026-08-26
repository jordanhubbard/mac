# ADR 0027: Upgrades are versioned, ordered, and fail closed

- Status: Proposed
- Date: 2026-08-21
- Decision owner: MAC fleet owner
- Related: [ADR 0013](0013-authoritative-hub-allocator.md) — the hub is the
  authority, so the hub owns the schema it writes

## Stage 1A implementation

Schema application is an explicit deploy operation:

    mac-schema-migrate --applied-by deploy:<release>

Existing fleet authorities that predate the ordered ledger additionally require
`--authorize-existing-baseline`. That flag does not waive validation: the
runner refuses partial or unknown schemas, applies every migration and its
postcondition in one transaction, and records the stable ID and SHA-256 only
after proof succeeds. Hub startup only verifies; it does not repeatedly execute
the schema bundle. Fleet deploy first runs a read-only preflight; if work is
pending it requires the typed quiescence proof, creates and restore-verifies a
PostgreSQL backup, records the backup-bound deploy receipt, then migrates before
any hub supervisor starts. No verified backup means no migration.

## Context

mac has one deployment today, and its project instructions say so: break schemas
and bump versions rather than carrying shims, because there are no other users.
That stance is correct right now and **expires the moment someone else deploys**.
This ADR is what replaces it, and it should land before the first external user
rather than after.

### What exists, measured 2026-08-21

- `mac.__version__` is single-sourced (`1.1.0`), and `pyproject.toml` derives
  from it. The **code** knows its version.
- The **database does not**. `schema_version` appears in `schema.sql` only as a
  column on two tables — payload metadata, not a database-level version. There
  is no version table, no `alembic`, and no version check on startup.
- `PostgresStore.initialize()` re-applies the bundled schema on every start.
  It is idempotent by construction: every statement is `IF NOT EXISTS` or
  `OR REPLACE`.
- Thirty-two hand-written `ensure_column()` calls add columns that
  `schema.sql` cannot add to a live database.

So schema management today is **additive-only, unversioned and convergent**. It
converges an old database toward the current shape by adding what is missing.

That works, and it is not a migration system. It cannot express a rename, a
backfill, a type change, a split, or a delete. It cannot answer "what version is
this database" — so it cannot decide whether an upgrade is safe, and it cannot
refuse one that is not.

### Why convergence alone breaks on the first real migration

An additive converger is silent by design. Run new code against an old database
and it adds the missing columns and proceeds, which is exactly right until the
change is not additive. Then the code reads a column that exists but whose
*meaning* changed, or whose backfill never ran, and nothing anywhere says so.
The failure is not a crash; it is a system that runs and is wrong.

The same silence bit this project all week in smaller ways: a stale lock nothing
aged out, a dead pipeline nothing announced, a CLI running 54 commits behind
whose missing flags were indistinguishable from features that did not exist.
Unversioned upgrade is that pattern applied to the data.

## Decision

### 1. The database records its own version

A single row, written by the hub, naming the schema version the database is at.
Not a column on a payload table — a fact about the store. Every migration
updates it; nothing else does.

### 2. Migrations are ordered, named, and individually recorded

Each migration has an identifier and a direction, and the database records which
have been applied. "What version is this" is then answerable, and so is "what
would this upgrade do".

`ensure_column()` convergence remains for the additive case: it is cheap, it is
already there, and most changes really are just a new column. It becomes one
kind of migration rather than the only mechanism.

### 3. Newer database than code: refuse to start

If the store is at a version this binary does not know, the hub **stops with a
clear message** rather than proceeding. A rolled-back binary meeting a migrated
database is the case that silently corrupts, and it is precisely what happens
during an incident when someone reverts to "the version that worked".

### 4. Older database than code: migrate, but say so

Auto-migration on startup is the right default for a single-machine install and
the wrong one for a fleet without warning. So: detect always, report always,
apply automatically only when the operator has said it may — and never
mid-flight on a hub that is currently dispatching.

### 5. A migration that cannot be proved is not applied

Every migration is a transaction with a check that says whether it landed. If
the check cannot run, the migration does not run. This is the same rule the
cohort transaction journal already applies to deployments, for the same reason:
"probably migrated" must be distinguishable from "migrated".

### 6. Upgrade is a fleet event, not a process event

A fleet is N nodes and one control plane. A schema migration is a fleet-wide
transition and belongs in the machinery that already coordinates those — the
same cohort transaction the deploy uses — rather than happening independently in
whichever process restarts first.

### 7. Update detection is separate from update application

Knowing a newer release exists is cheap, local and safe. Applying it is neither.
They are two decisions and the tooling should keep them apart: report drift by
default, act only on instruction. `mac` already warns when its CLI runs from a
source checkout behind its upstream; a released install deserves the equivalent.

### 8. The baseline is a PRUNED schema, not the accreted one

Declaring today's shape as v1 would freeze every historical artefact into the
contract new users upgrade from. Measured on the live control plane, 2026-08-21:

    165 tables
     69 empty  (42%) -- of which 27 are work_package_*, 14 are fleet_*

An entire 27-table subsystem has never held a row. Baselining that means every
future migration carries it, every reader wonders what it is for, and the first
external operator inherits a schema whose largest coherent feature is unused.

So pruning precedes the baseline. Two rules, because "empty" is not the same as
"dead":

- **Empty AND unwritten is dead.** A table with no rows *and* no code path that
  inserts into it is an artefact. Drop it.
- **Empty but written is young.** A table a live code path writes to is a
  feature that has not fired yet, not a leftover. Keep it, and say which it is.

The same applies to columns: an attribute that is NULL in every row and set by
no writer is not a nullable field, it is a fossil. The 32 `ensure_column()`
calls are the record of what was added over time; nothing has ever recorded what
stopped being used.

This is the last moment it is cheap. There is exactly one deployment today, so a
destructive prune costs a backup and an afternoon. After release it costs a
migration, a deprecation window, and someone else's data.

### 9. Agents are upgraded too, and they are not the database

A fleet upgrade has three moving parts, and only one of them is the schema:

- the **control plane** — the store, covered by §1–§6;
- the **agent runtime** on each node — the code that claims and executes;
- the **agent's own state** — soul, memory, config, credentials, the things
  that make an agent that agent rather than a fresh one.

The third is the one with no story today. Moving an agent between hosts already
has a command; moving an agent across a *version* does not. An upgrade that
resets an agent's memory or drops its soul has destroyed the thing the fleet
accumulates, and it would do so silently, because nothing versions that state
either.

So: an agent declares the runtime version it is at, the hub knows what it
expects, and a mismatch is reported rather than assumed compatible. Agent-owned
state migrates under the same rules as the store — ordered, recorded, proved,
fail-closed — or it is explicitly declared version-independent. Which of the two
each artefact is should be written down before the first upgrade, not discovered
during one.

## Consequences

- mac becomes upgradable by someone who did not write it, which is the
  precondition for having users at all.
- Non-additive changes become possible. Today a rename is effectively forbidden
  because nothing can carry data across it.
- A version table and a migration ledger are new state to maintain, and a
  migration that is wrong is now recorded as applied. That is the trade: silent
  convergence never lies about what it did because it never claimed anything.
- The project's current "no external compat burden" stance narrows to "no
  compat burden for versions before the pruned baseline", and needs saying
  explicitly in `CLAUDE.md` rather than being inferred from having no users.
- Pruning is destructive and happens once, against a live store with 532,204
  rows of task history. It needs a backup taken and verified first, and the drop
  list reviewed by a human, because the cost of dropping a table that turns out
  to be young is much higher than leaving one that turns out to be dead.
- Startup gains a failure mode it did not have: refusing to run. §3 is a
  deliberate choice to fail loudly at the one moment the alternative is silent
  data damage.

## Alternatives considered

**Keep additive convergence only.** Rejected: it cannot express the migrations a
released product needs, and its silence is a feature only while every change is
additive.

**Adopt Alembic (or similar) wholesale.** Not rejected, and deliberately not
decided here. The decisions above — a version of record, fail-closed on
downgrade, fleet-coordinated application, proof per migration — are what matter
and are true whichever tool implements them. Choosing the tool is the next step,
and it should be made against these constraints rather than before them.

**Migrate automatically and silently, as most desktop apps do.** Rejected for a
fleet. A single-user application can migrate on launch because one process owns
the data. A control plane with N workers mid-task cannot, and the failure mode
is not a bad launch but a partially migrated fleet.
