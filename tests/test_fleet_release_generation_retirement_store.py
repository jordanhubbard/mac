"""Persistence tests for the deploy-generation retirement record.

`fleet_release_epoch_agents.generation` is the string the deploy script writes
into the node-local barrier file and that a worker reads back before it decides
whether it is still inside a release. Nothing recorded that a generation had
stopped being live once its epoch reached a terminal state, so an aborted
release left every participant draining behind a barrier no epoch would ever
satisfy. `fleet_release_generation_retirements` is that missing authority.

Three things are covered, because three different failure modes exist:

* fresh-create -- a new authority gets the table, its key and its index;
* migration-from-existing-DB -- an authority that predates the change, and one
  that carries an earlier PARTIAL version of the table, both come out whole
  after `initialize()`, which is the case `CREATE TABLE IF NOT EXISTS` silently
  skips and only the `ensure_column` mirror repairs;
* accessor round-trip -- what the next child will actually call.

Postgres is the only backend (`src/mac/store.py` explains why the SQLite one was
removed), so there is one parametrization, not two: these tests use the same
`ephemeral_store` / per-test-schema fixtures as the rest of the store suite and
skip in exactly the same circumstances.

This task is persistence only. Nothing here asserts abort or commit behaviour --
that deliberately does not change yet.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mac.test_support import create_schema, drop_store, ephemeral_store


TABLE = "fleet_release_generation_retirements"


def _schema_sql() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "src" / "mac" / "data" / "postgres" / "schema.sql").read_text()


def _columns(store, table: str = TABLE) -> set:
    rows = store.query_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ?",
        (table,),
    )
    return {row["column_name"] for row in rows}


@pytest.fixture()
def store():
    s = ephemeral_store()
    yield s
    s.close()


# --------------------------------------------------------------------------
# Fresh create
# --------------------------------------------------------------------------


def test_fresh_initialize_creates_the_retirement_table(store) -> None:
    assert _columns(store) == {
        "epoch_id",
        "agent_id",
        "generation",
        "terminal_state",
        "disposition",
        "reason",
        "prepared_at",
        "retired_at",
        "created_at",
    }


def test_fresh_initialize_creates_the_agent_generation_index(store) -> None:
    """The worker-facing lookup is (agent_id, generation); it must be indexed.

    Without this the barrier check degrades to a sequential scan of every
    retirement the fleet has ever recorded, on the hot path of every worker
    deciding whether to keep draining.
    """
    rows = store.query_all(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = current_schema() AND tablename = ?",
        (TABLE,),
    )
    definitions = [row["indexdef"] for row in rows]
    assert any(
        "agent_id" in d and "generation" in d and "retired_at" in d
        for d in definitions
    ), definitions


def test_the_primary_key_is_the_participation(store) -> None:
    """One retirement per (epoch, agent, generation) participation."""
    store.execute(
        "INSERT INTO %s (epoch_id, agent_id, generation, terminal_state, "
        "disposition, reason, prepared_at, retired_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)" % TABLE,
        ("ep1", "agent_a", "gen-1", "aborted", "", "", "t0", "t1", "t1"),
    )
    from mac.store import StoreError

    with pytest.raises(StoreError):
        store.execute(
            "INSERT INTO %s (epoch_id, agent_id, generation, terminal_state, "
            "disposition, reason, prepared_at, retired_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)" % TABLE,
            ("ep1", "agent_a", "gen-1", "committed", "", "", "t0", "t2", "t2"),
        )


def test_terminal_state_is_constrained_to_the_two_outcomes(store) -> None:
    from mac.store import StoreError

    with pytest.raises(StoreError):
        store.execute(
            "INSERT INTO %s (epoch_id, agent_id, generation, terminal_state, "
            "disposition, reason, prepared_at, retired_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)" % TABLE,
            ("ep1", "agent_a", "gen-1", "open", "", "", "t0", "t1", "t1"),
        )


def test_schema_declares_the_additive_migration_for_every_added_column() -> None:
    """Fresh-only DDL is the drift that reached the live hub once already.

    `reviews.findings` was declared in a CREATE TABLE, reached every fresh
    database, and had to be ALTERed onto the running authority by hand mid-deploy
    because nothing upgraded the existing one. Each column added here carries the
    matching `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
    """
    text = _schema_sql()
    for column in (
        "terminal_state",
        "disposition",
        "reason",
        "prepared_at",
        "retired_at",
        "created_at",
    ):
        assert re.search(
            r"ALTER TABLE %s\s+ADD COLUMN IF NOT EXISTS\s+%s" % (TABLE, column),
            text,
        ), "%s.%s lacks an additive migration" % (TABLE, column)


def test_store_postgres_mirrors_every_additive_column() -> None:
    """The ALTERs in schema.sql and the ensure_column calls must agree.

    schema.sql only runs where the packaged DDL is applied; `initialize()` is
    what a live hub goes through. A column present in one and not the other is a
    backend that upgrades on some paths and not others.
    """
    source = (
        Path(__file__).resolve().parents[1] / "src" / "mac" / "store_postgres.py"
    ).read_text()
    mirrored = set(
        re.findall(
            r'ensure_column\(\s*"%s",\s*"(\w+)"' % TABLE,
            source,
        )
    )
    declared = set(
        re.findall(
            r"ALTER TABLE %s\s+ADD COLUMN IF NOT EXISTS\s+(\w+)" % TABLE,
            _schema_sql(),
        )
    )
    assert declared, "no additive migrations found -- the scan is broken"
    assert mirrored == declared, (
        "schema.sql and store_postgres.py disagree: %s"
        % sorted(mirrored ^ declared)
    )


# --------------------------------------------------------------------------
# Migration from an existing database
# --------------------------------------------------------------------------


def _store_without_schema():
    """A `PostgresStore` on an empty schema, with the DDL NOT yet applied."""
    from mac.store_postgres import PostgresStore

    schema, scoped = create_schema()
    store = PostgresStore(scoped, pool_size=2, min_size=1)
    store._mac_test_schema = schema
    return store


def test_initialize_adds_the_table_to_an_authority_that_lacks_it() -> None:
    """The ordinary upgrade: a hub that predates the table gets it."""
    store = _store_without_schema()
    try:
        assert _columns(store) == set(), "schema was applied too early"
        store.initialize()
        assert "terminal_state" in _columns(store)
    finally:
        drop_store(store)


def test_initialize_repairs_a_partial_pre_existing_table() -> None:
    """The upgrade `CREATE TABLE IF NOT EXISTS` cannot do on its own.

    An authority carrying an earlier, narrower version of the table is skipped
    entirely by the CREATE, so only the `ensure_column` mirror in
    `initialize()` widens it. The pre-existing row must survive -- these are
    durable facts, not a cache -- which is why every NOT NULL addition carries a
    DEFAULT.
    """
    store = _store_without_schema()
    try:
        store.execute(
            "CREATE TABLE %s ("
            "  epoch_id TEXT NOT NULL,"
            "  agent_id TEXT NOT NULL,"
            "  generation TEXT NOT NULL,"
            "  PRIMARY KEY (epoch_id, agent_id, generation)"
            ")" % TABLE
        )
        store.execute(
            "INSERT INTO %s (epoch_id, agent_id, generation) VALUES (?, ?, ?)"
            % TABLE,
            ("ep_old", "agent_old", "gen-old"),
        )

        store.initialize()

        assert _columns(store) >= {
            "epoch_id",
            "agent_id",
            "generation",
            "terminal_state",
            "disposition",
            "reason",
            "prepared_at",
            "retired_at",
            "created_at",
        }
        row = store.query_one(
            "SELECT * FROM %s WHERE epoch_id = ?" % TABLE, ("ep_old",)
        )
        assert row is not None, "the migration dropped a durable fact"
        assert row["generation"] == "gen-old"
        assert row["terminal_state"] == ""
    finally:
        drop_store(store)


def test_initialize_is_idempotent_over_the_new_table() -> None:
    store = _store_without_schema()
    try:
        store.initialize()
        before = _columns(store)
        store.initialize()
        assert _columns(store) == before
    finally:
        drop_store(store)


# --------------------------------------------------------------------------
# Accessor round-trip
# --------------------------------------------------------------------------


def test_record_then_read_back_the_retirement(store) -> None:
    store.record_fleet_release_generation_retirement(
        epoch_id="ep_1",
        agent_id="agent_a",
        generation="gen-1",
        terminal_state="aborted",
        prepared_at="2026-08-21T09:00:00+00:00",
        retired_at="2026-08-21T09:05:00+00:00",
        disposition="rolled_back",
        reason="prove step timed out",
    )
    row = store.get_fleet_release_generation_retirement("agent_a", "gen-1")
    assert row is not None
    assert row["epoch_id"] == "ep_1"
    assert row["terminal_state"] == "aborted"
    assert row["disposition"] == "rolled_back"
    assert row["reason"] == "prove step timed out"
    assert row["prepared_at"] == "2026-08-21T09:00:00+00:00"
    assert row["retired_at"] == "2026-08-21T09:05:00+00:00"
    assert row["created_at"]


def test_unretired_generation_reads_as_none(store) -> None:
    """Absence must not read as permission to stop draining."""
    assert store.get_fleet_release_generation_retirement("agent_a", "gen-1") is None
    store.record_fleet_release_generation_retirement(
        epoch_id="ep_1",
        agent_id="agent_a",
        generation="gen-1",
        terminal_state="committed",
        prepared_at="t0",
        retired_at="t1",
    )
    # A different generation for the same agent is still live.
    assert store.get_fleet_release_generation_retirement("agent_a", "gen-2") is None
    # ...as is the same generation on a different agent.
    assert store.get_fleet_release_generation_retirement("agent_b", "gen-1") is None


def test_blank_lookup_arguments_read_as_none(store) -> None:
    assert store.get_fleet_release_generation_retirement("", "gen-1") is None
    assert store.get_fleet_release_generation_retirement("agent_a", "  ") is None


def test_replaying_a_terminal_transition_updates_in_place(store) -> None:
    """A retried abort must not append a second, contradictory fact."""
    for reason in ("first pass", "second pass"):
        store.record_fleet_release_generation_retirement(
            epoch_id="ep_1",
            agent_id="agent_a",
            generation="gen-1",
            terminal_state="aborted",
            prepared_at="t0",
            retired_at="t1",
            reason=reason,
        )
    rows = store.query_all("SELECT * FROM %s" % TABLE)
    assert len(rows) == 1
    assert rows[0]["reason"] == "second pass"


def test_the_newest_retirement_wins(store) -> None:
    """One agent can carry the same generation through more than one epoch."""
    store.record_fleet_release_generation_retirement(
        epoch_id="ep_1",
        agent_id="agent_a",
        generation="gen-1",
        terminal_state="aborted",
        prepared_at="2026-08-21T09:00:00+00:00",
        retired_at="2026-08-21T09:05:00+00:00",
    )
    store.record_fleet_release_generation_retirement(
        epoch_id="ep_2",
        agent_id="agent_a",
        generation="gen-1",
        terminal_state="committed",
        prepared_at="2026-08-21T10:00:00+00:00",
        retired_at="2026-08-21T10:05:00+00:00",
    )
    row = store.get_fleet_release_generation_retirement("agent_a", "gen-1")
    assert row is not None
    assert row["epoch_id"] == "ep_2"
    assert row["terminal_state"] == "committed"


def test_recording_participates_in_the_callers_transaction(store) -> None:
    """The retirement and the terminal transition must not be separable.

    The next child records the retirement inside the same transaction that moves
    the epoch to `aborted`/`committed`. If that transaction rolls back, the
    retirement must roll back with it -- otherwise a crash mid-abort can retire a
    generation whose epoch is still live, which releases workers early.
    """

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with store.transaction() as conn:
            store.record_fleet_release_generation_retirement(
                epoch_id="ep_1",
                agent_id="agent_a",
                generation="gen-1",
                terminal_state="aborted",
                prepared_at="t0",
                retired_at="t1",
                conn=conn,
            )
            raise _Boom("terminal transition failed after the retirement write")

    assert store.get_fleet_release_generation_retirement("agent_a", "gen-1") is None

    with store.transaction() as conn:
        store.record_fleet_release_generation_retirement(
            epoch_id="ep_1",
            agent_id="agent_a",
            generation="gen-1",
            terminal_state="aborted",
            prepared_at="t0",
            retired_at="t1",
            conn=conn,
        )
    assert store.get_fleet_release_generation_retirement("agent_a", "gen-1") is not None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"epoch_id": ""},
        {"agent_id": "   "},
        {"generation": ""},
        {"terminal_state": "open"},
        {"terminal_state": ""},
        {"retired_at": ""},
    ],
)
def test_invalid_retirements_are_rejected_before_the_write(store, kwargs) -> None:
    """A bad value is a ValueError naming the offender, not a driver error."""
    call = {
        "epoch_id": "ep_1",
        "agent_id": "agent_a",
        "generation": "gen-1",
        "terminal_state": "aborted",
        "prepared_at": "t0",
        "retired_at": "t1",
    }
    call.update(kwargs)
    with pytest.raises(ValueError):
        store.record_fleet_release_generation_retirement(**call)
    assert store.query_all("SELECT * FROM %s" % TABLE) == []


def test_the_helpers_are_on_the_store_protocol() -> None:
    """Declared on the protocol, so a backend that lacks one fails the check.

    Sixteen helpers once lived on the store implementation while the protocol
    declared only the primitives; `isinstance(store, Store)` passed and the
    endpoint that needed them returned 500 in production.
    """
    from mac.store import Store
    from mac.store_postgres import PostgresStore

    for name in (
        "record_fleet_release_generation_retirement",
        "get_fleet_release_generation_retirement",
    ):
        assert hasattr(Store, name), "protocol does not declare %s" % name
        assert callable(getattr(PostgresStore, name, None)), (
            "PostgresStore is missing %s" % name
        )
