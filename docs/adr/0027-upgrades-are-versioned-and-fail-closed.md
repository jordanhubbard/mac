# ADR 0027: Upgrades are versioned, ordered, and fail closed

- Status: Proposed
- Date: 2026-08-21
- Decision owner: MAC fleet owner
- Related: [ADR 0013](0013-authoritative-hub-allocator.md) — the hub is the
  authority, so the hub owns the schema it writes

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

## Consequences

- mac becomes upgradable by someone who did not write it, which is the
  precondition for having users at all.
- Non-additive changes become possible. Today a rename is effectively forbidden
  because nothing can carry data across it.
- A version table and a migration ledger are new state to maintain, and a
  migration that is wrong is now recorded as applied. That is the trade: silent
  convergence never lies about what it did because it never claimed anything.
- The project's current "no external compat burden" stance narrows to "no
  compat burden for versions before 1.x", and needs saying explicitly in
  `CLAUDE.md` rather than being inferred from having no users.
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
