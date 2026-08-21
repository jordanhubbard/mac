"""Persistence tests for the deploy-generation retirement record.

`fleet_release_epoch_agents.generation` is the exact string
`deploy/deploy-mac-fleet.sh` writes into the node-local barrier file and
`worker.py._deployment_barrier_state` reads back. Nothing recorded that a
generation had stopped being live once its epoch reached a terminal state, so a
worker holding a barrier from an aborted epoch had no authority to consult and
drained forever.

These tests cover the three things a later child depends on: that a fresh
database comes up with the table, that a hub which predates it (or carries a
narrower version of it) converges on the next `initialize()`, and that the two
accessors round-trip the fact. They deliberately do not touch abort or commit
behaviour -- this layer only has to be there when the caller lands.
"""

from __future__ import annotations

import pytest

from mac.test_support import ephemeral_store


TABLE = "fleet_release_generation_retirements"

#: The committed shape, asserted independently of schema.sql rather than parsed
#: back out of it -- a test that reads its expectation from the file it is
#: checking cannot fail.
EXPECTED_COLUMNS = frozenset(
    {
        "agent_id",
        "generation",
        "epoch_id",
        "state",
        "disposition",
        "reason",
        "prepared_at",
        "retired_at",
        "created_at",
    }
)


@pytest.fixture()
def store():
    s = ephemeral_store()
    try:
        yield s
    finally:
        s.close()


def _columns(store) -> set:
    return {
        row["column_name"]
        for row in store.query_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? AND table_schema = current_schema()",
            (TABLE,),
        )
    }


def _indexes(store) -> set:
    return {
        row["indexname"]
        for row in store.query_all(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = ? AND schemaname = current_schema()",
            (TABLE,),
        )
    }


def _record(store, **overrides) -> dict:
    """Write one retirement, returning the arguments it was written with."""
    payload = {
        "agent_id": "agent_alpha",
        "generation": "gen-2026-08-21T09:00:00Z",
        "epoch_id": "epoch_one",
        "state": "aborted",
        "retired_at": "2026-08-21T10:00:00+00:00",
        "disposition": "rolled_back",
        "reason": "prove step timed out",
        "prepared_at": "2026-08-21T09:00:00+00:00",
    }
    payload.update(overrides)
    conn = payload.pop("conn", None)
    store.record_fleet_release_generation_retirement(conn=conn, **payload)
    return payload


# -- fresh create -------------------------------------------------------


def test_a_fresh_database_has_the_retirement_table(store) -> None:
    assert _columns(store) == set(EXPECTED_COLUMNS)


def test_a_fresh_database_has_the_lookup_index(store) -> None:
    assert "idx_fleet_release_generation_retirements_lookup" in _indexes(store)


def test_the_primary_key_is_agent_generation_epoch(store) -> None:
    """Two epochs may retire the same generation; one epoch may not do it twice."""
    row = store.query_one(
        """
        SELECT string_agg(a.attname, ',' ORDER BY k.ordinality) AS cols
        FROM pg_constraint c
        JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality)
            ON TRUE
        JOIN pg_attribute a
            ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'p'
          AND c.conrelid = (current_schema() || '.' || ?)::regclass
        """,
        (TABLE,),
    )
    assert row["cols"] == "agent_id,generation,epoch_id"


def test_a_non_terminal_state_is_rejected_by_the_database(store) -> None:
    """The CHECK is the backstop under the accessor's own validation."""
    from mac.store import StoreError

    with pytest.raises(StoreError):
        store.execute(
            "INSERT INTO %s (agent_id, generation, epoch_id, state, "
            "disposition, retired_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)" % TABLE,
            ("agent_alpha", "gen-a", "epoch_one", "open", "", "t", "t"),
        )


# -- migration from an existing database --------------------------------


def test_initialize_adds_the_table_to_a_hub_that_predates_it(store) -> None:
    """A hub that upgraded from before the table gets it on the next start."""
    store.execute("DROP TABLE %s" % TABLE)
    assert _columns(store) == set()

    store.initialize()

    assert _columns(store) == set(EXPECTED_COLUMNS)
    assert "idx_fleet_release_generation_retirements_lookup" in _indexes(store)
    _record(store)
    assert store.get_fleet_release_generation_retirement(
        "agent_alpha", "gen-2026-08-21T09:00:00Z"
    ) is not None


def test_initialize_widens_a_narrower_table_and_keeps_its_rows(store) -> None:
    """The half-migrated shape: the CREATE landed, the later columns did not.

    `CREATE TABLE IF NOT EXISTS` is a no-op against it, so without the additive
    migrations the table would stay narrow and every write would fail on a
    missing column -- the way `reviews.findings` had to be ALTERed in by hand
    on the live hub mid-deploy.
    """
    store.execute("DROP TABLE %s" % TABLE)
    store.execute(
        "CREATE TABLE %s ("
        "agent_id TEXT NOT NULL, generation TEXT NOT NULL, "
        "epoch_id TEXT NOT NULL, "
        "PRIMARY KEY (agent_id, generation, epoch_id))" % TABLE
    )
    store.execute(
        "INSERT INTO %s (agent_id, generation, epoch_id) VALUES (?, ?, ?)"
        % TABLE,
        ("agent_legacy", "gen-legacy", "epoch_legacy"),
    )

    store.initialize()

    assert _columns(store) == set(EXPECTED_COLUMNS)
    # The pre-existing row survives; NOT NULL columns are backfilled with the
    # declared defaults rather than rejecting the ALTER.
    legacy = store.query_one(
        "SELECT * FROM %s WHERE agent_id = ?" % TABLE, ("agent_legacy",)
    )
    assert legacy is not None
    assert legacy["state"] == ""
    assert legacy["retired_at"] == ""
    assert legacy["reason"] is None
    # And the widened table takes real writes.
    _record(store)
    assert store.get_fleet_release_generation_retirement(
        "agent_alpha", "gen-2026-08-21T09:00:00Z"
    ) is not None


def test_initialize_is_idempotent_over_the_retirement_table(store) -> None:
    written = _record(store)
    store.initialize()
    store.initialize()
    assert _columns(store) == set(EXPECTED_COLUMNS)
    row = store.get_fleet_release_generation_retirement(
        written["agent_id"], written["generation"]
    )
    assert row is not None and row["epoch_id"] == "epoch_one"


# -- accessor round-trip ------------------------------------------------


def test_round_trip_returns_every_recorded_field(store) -> None:
    written = _record(store)
    row = store.get_fleet_release_generation_retirement(
        written["agent_id"], written["generation"]
    )
    assert row is not None
    assert row["agent_id"] == written["agent_id"]
    assert row["generation"] == written["generation"]
    assert row["epoch_id"] == "epoch_one"
    assert row["state"] == "aborted"
    assert row["disposition"] == "rolled_back"
    assert row["reason"] == "prove step timed out"
    assert row["prepared_at"] == "2026-08-21T09:00:00+00:00"
    assert row["retired_at"] == "2026-08-21T10:00:00+00:00"
    assert row["created_at"]


def test_a_live_generation_has_no_retirement(store) -> None:
    """None is the answer a worker on a still-live generation must get."""
    _record(store)
    assert (
        store.get_fleet_release_generation_retirement("agent_alpha", "gen-other")
        is None
    )
    assert (
        store.get_fleet_release_generation_retirement("agent_beta", "gen-2026-08-21T09:00:00Z")
        is None
    )


def test_the_lookup_is_scoped_to_one_agent(store) -> None:
    """Two nodes on the same generation retire independently."""
    _record(store, agent_id="agent_alpha", state="aborted")
    _record(store, agent_id="agent_beta", state="committed", disposition="applied")
    alpha = store.get_fleet_release_generation_retirement(
        "agent_alpha", "gen-2026-08-21T09:00:00Z"
    )
    beta = store.get_fleet_release_generation_retirement(
        "agent_beta", "gen-2026-08-21T09:00:00Z"
    )
    assert alpha["state"] == "aborted"
    assert beta["state"] == "committed"


def test_the_newest_retirement_wins(store) -> None:
    """A generation replayed under a later epoch keeps both facts, newest first."""
    _record(
        store,
        epoch_id="epoch_one",
        state="aborted",
        retired_at="2026-08-21T10:00:00+00:00",
    )
    _record(
        store,
        epoch_id="epoch_two",
        state="committed",
        disposition="applied",
        retired_at="2026-08-21T12:00:00+00:00",
    )
    # Written out of order relative to the clock, to prove the read orders by
    # retired_at rather than by insertion.
    _record(
        store,
        epoch_id="epoch_zero",
        state="aborted",
        retired_at="2026-08-21T08:00:00+00:00",
    )

    row = store.get_fleet_release_generation_retirement(
        "agent_alpha", "gen-2026-08-21T09:00:00Z"
    )
    assert row["epoch_id"] == "epoch_two"
    assert row["state"] == "committed"
    assert (
        store.query_one(
            "SELECT COUNT(*) AS n FROM %s WHERE agent_id = ?" % TABLE,
            ("agent_alpha",),
        )["n"]
        == 3
    )


def test_recording_the_same_epoch_twice_updates_in_place(store) -> None:
    """A retried terminal transition must refresh the row, not raise."""
    _record(store, state="aborted", reason="first", retired_at="2026-08-21T10:00:00+00:00")
    _record(
        store,
        state="committed",
        disposition="applied",
        reason="second",
        retired_at="2026-08-21T11:00:00+00:00",
    )
    assert (
        store.query_one(
            "SELECT COUNT(*) AS n FROM %s" % TABLE
        )["n"]
        == 1
    )
    row = store.get_fleet_release_generation_retirement(
        "agent_alpha", "gen-2026-08-21T09:00:00Z"
    )
    assert row["state"] == "committed"
    assert row["disposition"] == "applied"
    assert row["reason"] == "second"
    assert row["retired_at"] == "2026-08-21T11:00:00+00:00"


def test_optional_fields_default_rather_than_being_required(store) -> None:
    store.record_fleet_release_generation_retirement(
        agent_id="agent_alpha",
        generation="gen-minimal",
        epoch_id="epoch_one",
        state="committed",
        retired_at="2026-08-21T10:00:00+00:00",
    )
    row = store.get_fleet_release_generation_retirement("agent_alpha", "gen-minimal")
    assert row["disposition"] == ""
    assert row["reason"] is None
    assert row["prepared_at"] is None
    assert row["created_at"]


# -- transactional participation ----------------------------------------


def test_the_write_joins_the_callers_transaction(store) -> None:
    with store.transaction() as conn:
        _record(store, conn=conn)
    assert (
        store.get_fleet_release_generation_retirement(
            "agent_alpha", "gen-2026-08-21T09:00:00Z"
        )
        is not None
    )


def test_a_rolled_back_transaction_records_nothing(store) -> None:
    """The point of taking `conn`: the fact cannot outlive a failed transition."""

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with store.transaction() as conn:
            _record(store, conn=conn)
            raise _Boom("terminal transition failed after the retirement write")

    assert (
        store.get_fleet_release_generation_retirement(
            "agent_alpha", "gen-2026-08-21T09:00:00Z"
        )
        is None
    )


# -- accessor validation ------------------------------------------------


@pytest.mark.parametrize(
    "missing", ["agent_id", "generation", "epoch_id", "retired_at"]
)
def test_required_identifiers_are_rejected_when_blank(store, missing) -> None:
    payload = {
        "agent_id": "agent_alpha",
        "generation": "gen-a",
        "epoch_id": "epoch_one",
        "state": "aborted",
        "retired_at": "2026-08-21T10:00:00+00:00",
    }
    payload[missing] = "   "
    with pytest.raises(ValueError):
        store.record_fleet_release_generation_retirement(**payload)


@pytest.mark.parametrize("state", ["", "open", "proved", "ABORTED", None])
def test_a_non_terminal_state_is_rejected_by_the_accessor(store, state) -> None:
    """Only a terminal epoch retires a generation."""
    with pytest.raises(ValueError, match="state"):
        store.record_fleet_release_generation_retirement(
            agent_id="agent_alpha",
            generation="gen-a",
            epoch_id="epoch_one",
            state=state,
            retired_at="2026-08-21T10:00:00+00:00",
        )


@pytest.mark.parametrize(
    "agent_id,generation", [("", "gen-a"), ("agent_alpha", ""), ("", "")]
)
def test_a_blank_lookup_key_returns_none(store, agent_id, generation) -> None:
    assert (
        store.get_fleet_release_generation_retirement(agent_id, generation) is None
    )
