"""Durable retirement record for a deploy generation.

`fleet_release_epoch_agents.generation` is the string the deploy script writes
into the node-local barrier file and the worker reads back. Nothing recorded
that a generation had stopped being live once its epoch reached a terminal
state, so a worker behind an aborted epoch's barrier had no authority to
consult and drained forever. These tests cover the persistence layer that gives
it one: the table on a fresh database, the forward migration onto a database
that already has an older partial version of it, and the accessor round trip.

There is one backend -- `PostgresStore` -- so the "both backends" of the
original ask is the Postgres authority plus the `Store` protocol that declares
the accessors; the SQLite implementation was removed from the tree before this
work started.

Persistence only. Deciding *when* a generation is retired belongs to the epoch
service and is deliberately not touched here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mac.test_support import all_index_names, column_names, ephemeral_store, table_names


TABLE = "fleet_release_generation_retirements"

EXPECTED_COLUMNS = {
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


def _schema_sql() -> str:
    root = Path(__file__).resolve().parent.parent
    return (root / "src" / "mac" / "data" / "postgres" / "schema.sql").read_text()


@pytest.fixture()
def store():
    s = ephemeral_store()
    yield s


# ---------------------------------------------------------------------------
# Fresh create
# ---------------------------------------------------------------------------


def test_a_fresh_database_has_the_retirement_table(store) -> None:
    assert TABLE in table_names(store)


def test_a_fresh_database_has_every_declared_column(store) -> None:
    assert column_names(store, TABLE) == EXPECTED_COLUMNS


def test_the_lookup_index_exists(store) -> None:
    """(agent_id, generation) is the worker's question, so it must be indexed.

    The primary key is (epoch_id, agent_id) -- the participation row's key --
    which does not answer "what happened to this generation?" at all. Without
    this index that lookup is a sequential scan of every retirement the fleet
    has ever recorded.
    """
    assert "idx_fleet_release_generation_retirements_lookup" in all_index_names(store)
    indexed = store.query_one(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
        "AND indexname = ?",
        ("idx_fleet_release_generation_retirements_lookup",),
    )
    assert indexed is not None
    assert re.search(r"\(agent_id,\s*generation\)", indexed["indexdef"])


def test_terminal_state_is_constrained_at_the_database(store) -> None:
    """The vocabulary is enforced by the schema, not only by the accessor.

    The accessor validates too, but a service that reaches the table with raw
    SQL -- the epoch service writes its own -- must not be able to record a
    third kind of outcome that later readers have no rule for.
    """
    from mac.store import StoreError

    with pytest.raises(StoreError):
        store.execute(
            "INSERT INTO %s (epoch_id, agent_id, generation, terminal_state, "
            "retired_at, created_at) VALUES (?, ?, ?, ?, ?, ?)" % TABLE,
            ("ep_x", "agent_x", "gen_x", "abandoned", "t", "t"),
        )


def test_the_bundled_ddl_declares_the_table_and_index() -> None:
    """Guard the DDL text as well as the live database.

    `CREATE TABLE IF NOT EXISTS` is a no-op against an existing table and this
    repository has no migration framework, so a table that quietly leaves
    schema.sql still passes every live-database assertion above on a database
    that was created before the removal.
    """
    text = _schema_sql()
    assert re.search(r"CREATE TABLE IF NOT EXISTS\s+%s\s*\(" % TABLE, text)
    assert "idx_fleet_release_generation_retirements_lookup" in text


# ---------------------------------------------------------------------------
# Forward migration onto an existing database
# ---------------------------------------------------------------------------


def test_initialize_upgrades_a_partial_table_in_place(store) -> None:
    """A hub whose table predates the outcome columns gains them, keeping rows.

    This is the `reviews.findings` failure mode: a column declared in schema.sql
    that never reaches a database created before it existed, because
    CREATE TABLE IF NOT EXISTS does nothing to a table that is already there.
    The row written against the old shape must survive the upgrade -- an
    operator running a deploy cannot be asked to drop retirement history.
    """
    store.execute("DROP TABLE %s" % TABLE)
    store.execute(
        """
        CREATE TABLE %s (
            epoch_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            generation TEXT NOT NULL,
            PRIMARY KEY (epoch_id, agent_id)
        )
        """
        % TABLE
    )
    store.execute(
        "INSERT INTO %s (epoch_id, agent_id, generation) VALUES (?, ?, ?)" % TABLE,
        ("ep_old", "agent_old", "gen_old"),
    )
    assert column_names(store, TABLE) == {"epoch_id", "agent_id", "generation"}

    store.initialize()

    assert column_names(store, TABLE) == EXPECTED_COLUMNS
    survivor = store.query_one(
        "SELECT * FROM %s WHERE epoch_id = ?" % TABLE, ("ep_old",)
    )
    assert survivor is not None
    assert survivor["generation"] == "gen_old"
    # The added NOT NULL columns carry a default precisely so the existing row
    # can keep its place rather than blocking the ALTER.
    assert survivor["terminal_state"] == ""
    assert survivor["retired_at"] == ""


def test_initialize_is_idempotent(store) -> None:
    """Running the schema twice is what every hub restart does."""
    before = column_names(store, TABLE)
    store.initialize()
    store.initialize()
    assert column_names(store, TABLE) == before


# ---------------------------------------------------------------------------
# Accessor round trip
# ---------------------------------------------------------------------------


def _record(store, **overrides) -> dict:
    fields = {
        "epoch_id": "ep_1",
        "agent_id": "agent_1",
        "generation": "gen_1",
        "terminal_state": "aborted",
        "retired_at": "2026-08-21T00:00:00+00:00",
        "prepared_at": "2026-08-20T23:00:00+00:00",
        "disposition": "rolled_back",
        "reason": "prove step failed on node 3",
    }
    fields.update(overrides)
    store.record_fleet_release_generation_retirement(**fields)
    return fields


def test_a_recorded_retirement_reads_back_whole(store) -> None:
    written = _record(store)
    row = store.get_fleet_release_generation_retirement("agent_1", "gen_1")
    assert row is not None
    for key, value in written.items():
        assert row[key] == value, key
    assert row["created_at"]


def test_an_unrecorded_generation_reads_as_none(store) -> None:
    """Absence is *unknown*, not *live* -- the caller has to tell them apart."""
    _record(store)
    assert store.get_fleet_release_generation_retirement("agent_1", "gen_2") is None
    assert store.get_fleet_release_generation_retirement("agent_2", "gen_1") is None


def test_blank_lookup_arguments_read_as_none(store) -> None:
    _record(store)
    assert store.get_fleet_release_generation_retirement("", "gen_1") is None
    assert store.get_fleet_release_generation_retirement("agent_1", "  ") is None


def test_recording_the_same_epoch_twice_updates_in_place(store) -> None:
    """An abort path that retries must not leave two contradictory facts."""
    _record(store)
    _record(
        store,
        terminal_state="committed",
        disposition="completed",
        reason="rerun succeeded",
        retired_at="2026-08-21T01:00:00+00:00",
    )
    rows = store.query_all(
        "SELECT * FROM %s WHERE agent_id = ? AND generation = ?" % TABLE,
        ("agent_1", "gen_1"),
    )
    assert len(rows) == 1
    assert rows[0]["terminal_state"] == "committed"
    assert rows[0]["reason"] == "rerun succeeded"


def test_the_newest_retirement_wins_across_epochs(store) -> None:
    """One generation string can be carried through more than one epoch.

    Both rows are kept -- each is a true fact about its own epoch -- but the
    worker asking "is this generation still live?" gets the latest answer.
    """
    _record(store, epoch_id="ep_1", retired_at="2026-08-21T00:00:00+00:00")
    _record(
        store,
        epoch_id="ep_2",
        terminal_state="committed",
        retired_at="2026-08-21T02:00:00+00:00",
    )
    _record(store, epoch_id="ep_0", retired_at="2026-08-20T00:00:00+00:00")

    row = store.get_fleet_release_generation_retirement("agent_1", "gen_1")
    assert row is not None
    assert row["epoch_id"] == "ep_2"
    assert row["terminal_state"] == "committed"
    assert len(store.query_all("SELECT * FROM %s" % TABLE)) == 3


def test_a_retirement_can_join_a_callers_transaction(store) -> None:
    """The next child records this atomically with the epoch's terminal update.

    So the write has to accept an already-open connection: a retirement that
    commits while the abort it belongs to rolls back is worse than no record at
    all, because it retires a generation that is still live.
    """
    with store.transaction() as conn:
        store.record_fleet_release_generation_retirement(
            epoch_id="ep_txn",
            agent_id="agent_txn",
            generation="gen_txn",
            terminal_state="committed",
            retired_at="2026-08-21T03:00:00+00:00",
            conn=conn,
        )
    row = store.get_fleet_release_generation_retirement("agent_txn", "gen_txn")
    assert row is not None
    assert row["terminal_state"] == "committed"


def test_a_rolled_back_transaction_records_nothing(store) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with store.transaction() as conn:
            store.record_fleet_release_generation_retirement(
                epoch_id="ep_rb",
                agent_id="agent_rb",
                generation="gen_rb",
                terminal_state="aborted",
                retired_at="2026-08-21T04:00:00+00:00",
                conn=conn,
            )
            raise Boom()
    assert store.get_fleet_release_generation_retirement("agent_rb", "gen_rb") is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"epoch_id": ""},
        {"agent_id": "   "},
        {"generation": ""},
        {"terminal_state": "abandoned"},
        {"terminal_state": ""},
        {"retired_at": ""},
    ],
)
def test_an_unusable_retirement_is_refused(store, overrides) -> None:
    with pytest.raises(ValueError):
        _record(store, **overrides)
    assert store.query_all("SELECT * FROM %s" % TABLE) == []


def test_terminal_state_is_normalised(store) -> None:
    _record(store, terminal_state="ABORTED")
    row = store.get_fleet_release_generation_retirement("agent_1", "gen_1")
    assert row is not None
    assert row["terminal_state"] == "aborted"


def test_the_optional_columns_are_genuinely_optional(store) -> None:
    """A commit has no abort reason and no disposition; that must be writable."""
    store.record_fleet_release_generation_retirement(
        epoch_id="ep_min",
        agent_id="agent_min",
        generation="gen_min",
        terminal_state="committed",
        retired_at="2026-08-21T05:00:00+00:00",
    )
    row = store.get_fleet_release_generation_retirement("agent_min", "gen_min")
    assert row is not None
    assert row["disposition"] is None
    assert row["reason"] is None
    assert row["prepared_at"] is None
