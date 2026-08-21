"""Persistence tests for `fleet_release_generation_retirements`.

The table is the durable answer to a question a draining worker cannot answer
for itself: the generation string in its node-local
`$MAC_HOME/deploy-start-barrier` names a release epoch that has since aborted,
and without a recorded retirement the worker waits behind that barrier forever.

Covers the three paths a new table has to survive:

 * fresh create -- a database built from schema.sql has the table, its columns,
   its (agent_id, generation) index, and its constraints;
 * forward migration -- a database that predates the table gets it back when
   `PostgresStore.initialize()` re-applies the bundled DDL, which is the only
   migration mechanism this repository has;
 * accessor round-trip -- record/read, including the in-transaction write the
   abort and commit paths will use.

This task is the persistence layer only. Nothing here asserts anything about
when a retirement is written; that belongs to the child that changes the
abort/commit paths.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mac.store import StoreError
from mac.test_support import column_names, ephemeral_store, index_names, table_names


TABLE = "fleet_release_generation_retirements"
#: The primary key's backing index. It is the (agent_id, generation) index the
#: read path needs -- the key is ordered for that -- so there is no second one.
INDEX = TABLE + "_pkey"


def _schema_text() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "src" / "mac" / "data" / "postgres" / "schema.sql").read_text()


def _seed_agent(store, agent_id: str = "agent_alpha") -> str:
    """Insert the machine + agent rows the retirement's foreign key needs.

    Written as direct inserts rather than through ControlPlane because these
    are persistence tests: registering an agent for real would drag in the
    secret key, capability negotiation, and event emission, none of which this
    table has an opinion about.
    """
    machine_id = "machine_for_" + agent_id
    store.execute(
        "INSERT INTO machines (id, hostname, labels, resources, trusted, "
        "created_at, updated_at, last_seen_at) "
        "VALUES (?, ?, '{}', '{}', 1, ?, ?, ?) ON CONFLICT DO NOTHING",
        (machine_id, agent_id + "-host", "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    store.execute(
        "INSERT INTO agents (id, machine_id, name, capabilities, resources, "
        "status, health_status, created_at, updated_at, last_seen_at) "
        "VALUES (?, ?, ?, '[]', '{}', 'idle', 'healthy', ?, ?, ?) "
        "ON CONFLICT DO NOTHING",
        (agent_id, machine_id, agent_id, "2026-01-01T00:00:00+00:00",
         "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    return agent_id


def _seed_epoch(store, epoch_id: str, *, state: str = "aborted") -> str:
    store.execute(
        "INSERT INTO fleet_release_epochs (epoch_id, request_sha256, "
        "identity_sha256, identity_payload, state, policy_snapshot, actor, "
        "prepared_at) VALUES (?, ?, ?, '{}', ?, '{}', 'agent_publisher', ?) "
        "ON CONFLICT DO NOTHING",
        (
            epoch_id,
            "a" * 64,
            "sha-" + epoch_id,
            state,
            "2026-01-01T00:00:00+00:00",
        ),
    )
    return epoch_id


@pytest.fixture()
def store():
    s = ephemeral_store()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def seeded(store):
    """A store with one agent and two terminal epochs already present."""
    agent = _seed_agent(store)
    _seed_epoch(store, "epoch_one")
    _seed_epoch(store, "epoch_two", state="committed")
    return store, agent


# ---------------------------------------------------------------------------
# Fresh create
# ---------------------------------------------------------------------------


def test_fresh_database_has_the_retirement_table_and_columns(store) -> None:
    assert TABLE in table_names(store)
    assert column_names(store, TABLE) == {
        "agent_id",
        "generation",
        "epoch_id",
        "outcome",
        "disposition",
        "reason",
        "prepared_at",
        "retired_at",
        "created_at",
    }


def test_the_only_index_is_the_one_the_read_path_uses(store) -> None:
    """`(agent_id, generation)` is the whole read key, and one index serves it.

    The primary key is ordered to be that index. A second index carrying
    `retired_at` would be the speculative kind tests/test_no_dead_indexes.py
    was written to refuse, so its absence is asserted rather than assumed.
    """
    assert index_names(store, TABLE) == {INDEX}


def test_lookup_by_agent_and_generation_uses_the_index(seeded) -> None:
    store, agent = seeded
    plan = " ".join(
        str(dict(row).get("QUERY PLAN", ""))
        for row in store.query_all(
            "EXPLAIN SELECT * FROM " + TABLE + " "
            "WHERE agent_id = ? AND generation = ? "
            "ORDER BY retired_at DESC, epoch_id DESC LIMIT 1",
            (agent, "gen-a"),
        )
    )
    assert INDEX in plan, plan
    assert "Seq Scan" not in plan, plan


def test_outcome_is_constrained_to_terminal_epoch_states(seeded) -> None:
    """The CHECK, not just the helper's validation, rejects a junk outcome.

    A later caller may write this table with its own SQL inside an epoch
    transaction; the database has to be the one refusing 'open'.
    """
    store, agent = seeded
    with pytest.raises(StoreError):
        store.execute(
            "INSERT INTO " + TABLE + " (agent_id, generation, epoch_id, "
            "outcome, disposition, retired_at, created_at) "
            "VALUES (?, 'gen-a', 'epoch_one', 'open', 'released', ?, ?)",
            (agent, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )


def test_retirement_requires_a_known_agent(store) -> None:
    _seed_epoch(store, "epoch_one")
    with pytest.raises(StoreError):
        store.record_fleet_release_generation_retirement(
            agent_id="agent_never_registered",
            generation="gen-a",
            epoch_id="epoch_one",
            outcome="aborted",
            disposition="released",
            retired_at="2026-01-01T00:00:00+00:00",
            created_at="2026-01-01T00:00:00+00:00",
        )


# ---------------------------------------------------------------------------
# Forward migration
# ---------------------------------------------------------------------------


def test_initialize_restores_the_table_on_a_database_that_predates_it(
    seeded,
) -> None:
    """The forward migration for a hub that upgraded, not a fresh install.

    This repository has no migration framework: re-applying the bundled schema
    IS the migration, and it only works because every statement is `IF NOT
    EXISTS`. Dropping the table reproduces the pre-change database exactly, so
    a CREATE that was placed where initialize() never reaches it -- or an index
    left out of schema.sql and created by hand on somebody's laptop -- fails
    here rather than on the live hub.
    """
    store, agent = seeded
    store.execute("DROP TABLE " + TABLE)
    assert TABLE not in table_names(store)

    store.initialize()

    assert TABLE in table_names(store)
    assert INDEX in index_names(store, TABLE)
    # ...and the migrated table is writable, not merely present.
    store.record_fleet_release_generation_retirement(
        agent_id=agent,
        generation="gen-after-migration",
        epoch_id="epoch_one",
        outcome="aborted",
        disposition="released",
        retired_at="2026-01-01T00:00:00+00:00",
        created_at="2026-01-01T00:00:00+00:00",
    )
    row = store.get_fleet_release_generation_retirement(
        agent, "gen-after-migration"
    )
    assert row is not None and row["outcome"] == "aborted"


def test_initialize_is_idempotent_for_an_existing_retirement_table(
    seeded,
) -> None:
    """Re-running initialize() must not disturb rows already recorded."""
    store, agent = seeded
    store.record_fleet_release_generation_retirement(
        agent_id=agent,
        generation="gen-a",
        epoch_id="epoch_one",
        outcome="aborted",
        disposition="released",
        reason="publisher aborted the epoch",
        retired_at="2026-01-01T00:00:00+00:00",
        created_at="2026-01-01T00:00:00+00:00",
    )

    store.initialize()

    row = store.get_fleet_release_generation_retirement(agent, "gen-a")
    assert row is not None
    assert row["reason"] == "publisher aborted the epoch"


# ---------------------------------------------------------------------------
# Accessor round-trip
# ---------------------------------------------------------------------------


def test_record_then_read_back_every_recorded_field(seeded) -> None:
    store, agent = seeded
    store.record_fleet_release_generation_retirement(
        agent_id=agent,
        generation="gen-a",
        epoch_id="epoch_one",
        outcome="aborted",
        disposition="released",
        reason="cutover proof never arrived",
        prepared_at="2026-01-01T00:00:00+00:00",
        retired_at="2026-01-01T00:05:00+00:00",
        created_at="2026-01-01T00:05:00+00:00",
    )

    row = store.get_fleet_release_generation_retirement(agent, "gen-a")
    assert row is not None
    assert row["agent_id"] == agent
    assert row["generation"] == "gen-a"
    assert row["epoch_id"] == "epoch_one"
    assert row["outcome"] == "aborted"
    assert row["disposition"] == "released"
    assert row["reason"] == "cutover proof never arrived"
    assert row["prepared_at"] == "2026-01-01T00:00:00+00:00"
    assert row["retired_at"] == "2026-01-01T00:05:00+00:00"
    assert row["created_at"] == "2026-01-01T00:05:00+00:00"


def test_an_unretired_generation_reads_as_none(seeded) -> None:
    """Absence must be readable, because absence is the common case.

    A live generation and a generation this hub has never heard of look the
    same from here, so the caller has to treat None as "keep waiting".
    """
    store, agent = seeded
    assert store.get_fleet_release_generation_retirement(agent, "gen-live") is None
    assert (
        store.get_fleet_release_generation_retirement("agent_unknown", "gen-a")
        is None
    )


def test_committed_outcome_round_trips_too(seeded) -> None:
    """An epoch that committed also retires the generation it superseded."""
    store, agent = seeded
    store.record_fleet_release_generation_retirement(
        agent_id=agent,
        generation="gen-a",
        epoch_id="epoch_two",
        outcome="committed",
        disposition="superseded",
        retired_at="2026-01-01T00:05:00+00:00",
        created_at="2026-01-01T00:05:00+00:00",
    )
    row = store.get_fleet_release_generation_retirement(agent, "gen-a")
    assert row is not None
    assert (row["outcome"], row["disposition"]) == ("committed", "superseded")
    assert row["reason"] is None
    assert row["prepared_at"] is None


def test_the_newest_retirement_wins_when_a_generation_is_reused(seeded) -> None:
    """Same generation string, two epochs: the later retirement is the answer.

    Generations are opaque strings chosen by the deploy script, so nothing
    stops one recurring. Reading the older row would tell a worker the epoch
    aborted when the live one committed.
    """
    store, agent = seeded
    store.record_fleet_release_generation_retirement(
        agent_id=agent,
        generation="gen-a",
        epoch_id="epoch_one",
        outcome="aborted",
        disposition="released",
        retired_at="2026-01-01T00:05:00+00:00",
        created_at="2026-01-01T00:05:00+00:00",
    )
    store.record_fleet_release_generation_retirement(
        agent_id=agent,
        generation="gen-a",
        epoch_id="epoch_two",
        outcome="committed",
        disposition="superseded",
        retired_at="2026-01-02T00:00:00+00:00",
        created_at="2026-01-02T00:00:00+00:00",
    )

    row = store.get_fleet_release_generation_retirement(agent, "gen-a")
    assert row is not None
    assert row["epoch_id"] == "epoch_two"
    assert row["outcome"] == "committed"
    # Both facts are kept; the reader picks, the writer does not overwrite.
    rows = store.query_all(
        "SELECT epoch_id FROM " + TABLE + " WHERE agent_id = ? AND generation = ?",
        (agent, "gen-a"),
    )
    assert sorted(r["epoch_id"] for r in rows) == ["epoch_one", "epoch_two"]


def test_recording_the_same_terminal_transition_twice_updates_in_place(
    seeded,
) -> None:
    """The abort path can be retried; a retry must not append a second row."""
    store, agent = seeded
    for reason in ("first pass", "after retry"):
        store.record_fleet_release_generation_retirement(
            agent_id=agent,
            generation="gen-a",
            epoch_id="epoch_one",
            outcome="aborted",
            disposition="released",
            reason=reason,
            retired_at="2026-01-01T00:05:00+00:00",
            created_at="2026-01-01T00:05:00+00:00",
        )

    rows = store.query_all(
        "SELECT reason FROM " + TABLE + " WHERE agent_id = ? AND generation = ?",
        (agent, "gen-a"),
    )
    assert [r["reason"] for r in rows] == ["after retry"]


def test_generations_are_scoped_to_their_agent(seeded) -> None:
    store, agent = seeded
    other = _seed_agent(store, "agent_beta")
    store.record_fleet_release_generation_retirement(
        agent_id=other,
        generation="gen-a",
        epoch_id="epoch_one",
        outcome="aborted",
        disposition="released",
        retired_at="2026-01-01T00:05:00+00:00",
        created_at="2026-01-01T00:05:00+00:00",
    )
    assert store.get_fleet_release_generation_retirement(agent, "gen-a") is None
    assert store.get_fleet_release_generation_retirement(other, "gen-a") is not None


# ---------------------------------------------------------------------------
# In-transaction write
# ---------------------------------------------------------------------------


def test_record_participates_in_the_callers_transaction(seeded) -> None:
    """The retirement lands with the abort that caused it, not beside it."""
    store, agent = seeded
    with store.transaction() as conn:
        conn.execute(
            "UPDATE fleet_release_epochs SET state = 'aborted' WHERE epoch_id = ?",
            ("epoch_one",),
        )
        store.record_fleet_release_generation_retirement(
            agent_id=agent,
            generation="gen-a",
            epoch_id="epoch_one",
            outcome="aborted",
            disposition="released",
            retired_at="2026-01-01T00:05:00+00:00",
            created_at="2026-01-01T00:05:00+00:00",
            conn=conn,
        )
    assert store.get_fleet_release_generation_retirement(agent, "gen-a") is not None


def test_a_rolled_back_transaction_records_no_retirement(seeded) -> None:
    """The half that matters: an abort that fails must not retire anything.

    A retirement written outside the caller's transaction would survive the
    rollback and tell every worker holding that generation to stop waiting for
    a cutover that is still in flight.
    """
    store, agent = seeded

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with store.transaction() as conn:
            store.record_fleet_release_generation_retirement(
                agent_id=agent,
                generation="gen-a",
                epoch_id="epoch_one",
                outcome="aborted",
                disposition="released",
                retired_at="2026-01-01T00:05:00+00:00",
                created_at="2026-01-01T00:05:00+00:00",
                conn=conn,
            )
            raise _Boom("abort failed after the retirement was staged")

    assert store.get_fleet_release_generation_retirement(agent, "gen-a") is None


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_outcome_must_be_a_terminal_epoch_state(seeded) -> None:
    store, agent = seeded
    with pytest.raises(ValueError, match="outcome"):
        store.record_fleet_release_generation_retirement(
            agent_id=agent,
            generation="gen-a",
            epoch_id="epoch_one",
            outcome="open",
            disposition="released",
            retired_at="2026-01-01T00:05:00+00:00",
            created_at="2026-01-01T00:05:00+00:00",
        )


@pytest.mark.parametrize(
    "missing", ["agent_id", "generation", "epoch_id"]
)
def test_the_identifying_triple_is_required(seeded, missing: str) -> None:
    """An empty generation would record a retirement nothing can ever match."""
    store, agent = seeded
    kwargs = dict(
        agent_id=agent,
        generation="gen-a",
        epoch_id="epoch_one",
        outcome="aborted",
        disposition="released",
        retired_at="2026-01-01T00:05:00+00:00",
        created_at="2026-01-01T00:05:00+00:00",
    )
    kwargs[missing] = "   "
    with pytest.raises(ValueError):
        store.record_fleet_release_generation_retirement(**kwargs)


def test_lookup_with_a_blank_key_is_none_not_an_error(seeded) -> None:
    """A reader with nothing to ask about gets the same answer as a miss."""
    store, agent = seeded
    assert store.get_fleet_release_generation_retirement("", "gen-a") is None
    assert store.get_fleet_release_generation_retirement(agent, "") is None


# ---------------------------------------------------------------------------
# Schema source
# ---------------------------------------------------------------------------


def test_schema_declares_the_table_and_its_index() -> None:
    """schema.sql is the migration, so the DDL text is part of the contract."""
    schema = _schema_text()
    assert re.search(
        r"CREATE TABLE IF NOT EXISTS\s+" + TABLE + r"\s*\(", schema
    )
    # The key order is the index; asserting the text keeps a future edit that
    # reorders it (and silently drops the (agent_id, generation) prefix) honest.
    assert "PRIMARY KEY (agent_id, generation, epoch_id)" in schema
