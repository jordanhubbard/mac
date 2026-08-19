"""Durable deploy-generation retirement fact in the hub store.

Leaf persistence layer for the fix to "aborted fleet release epochs strand
every worker behind a stale deploy barrier". A worker writes its per-participant
``generation`` string into ``$MAC_HOME/deploy-start-barrier`` and reads it back
in ``worker.py`` ``_deployment_barrier_state``. Once an epoch reaches a terminal
state that generation is no longer live, but nothing recorded the fact, so the
worker had no authority to consult and drained forever.

These tests cover the smallest correct persistence shape for that authority: a
dedicated ``fleet_release_generation_retirements`` table keyed on
``(agent_id, generation)`` with the two accessors a later child will call --
record a terminal outcome (in or out of a transaction) and look up the newest
retirement for a ``(agent_id, generation)`` pair. They do NOT exercise
abort/commit behaviour; this task only adds the fact and its accessors.

The store is Postgres-only (SQLite was removed), so "both backends" collapses to
the single live backend and every case runs against the ephemeral Postgres the
rest of the store suite uses.
"""

from __future__ import annotations

import pytest

from mac.models import new_id, utcnow
from mac.test_support import (
    all_index_names,
    column_names,
    create_schema,
    ephemeral_store,
    store_on,
    table_names,
)

RETIREMENT_TABLE = "fleet_release_generation_retirements"


def _record_kwargs(**overrides):
    now = utcnow()
    kw = dict(
        agent_id="agent_" + new_id("x")[-8:],
        generation="deploy-gen-1",
        epoch_id="epoch_" + new_id("e")[-8:],
        terminal_state="aborted",
        retired_at=now,
        created_at=now,
        updated_at=now,
        disposition="retain_installed",
        reason="operator aborted the release",
        prepared_at=now,
    )
    kw.update(overrides)
    return kw


# ---------------------------------------------------------------------------
# Fresh-database table / column / index creation
# ---------------------------------------------------------------------------


class TestFreshDatabaseSchema:
    def test_table_exists(self) -> None:
        db = ephemeral_store()
        assert RETIREMENT_TABLE in table_names(db)
        db.close()

    def test_columns(self) -> None:
        db = ephemeral_store()
        cols = column_names(db, RETIREMENT_TABLE)
        expected = {
            "agent_id",
            "generation",
            "epoch_id",
            "terminal_state",
            "disposition",
            "reason",
            "prepared_at",
            "retired_at",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols)
        db.close()

    def test_index_on_agent_generation(self) -> None:
        db = ephemeral_store()
        indexes = all_index_names(db)
        assert "idx_fleet_release_generation_retire_agent_gen" in indexes
        db.close()


# ---------------------------------------------------------------------------
# Migration from a pre-existing database that predates the table
# ---------------------------------------------------------------------------


class TestMigrationFromExistingDatabase:
    def test_table_appears_after_reinitialize(self) -> None:
        """A database created before the table gains it when initialize re-runs.

        The bundled DDL uses ``CREATE TABLE IF NOT EXISTS`` for this fact, which
        is the forward migration: a live database that predates it acquires the
        table (and its indexes) the next time the schema is applied, without
        touching existing rows.
        """
        schema, dsn = create_schema()

        # A store that skips schema application stands in for the pre-existing
        # database. Drop the table so the DSN genuinely lacks it, mimicking a
        # database provisioned before this task landed.
        preexisting = store_on(dsn, initialize=False)
        preexisting.execute("DROP TABLE IF EXISTS %s" % RETIREMENT_TABLE)
        assert RETIREMENT_TABLE not in table_names(preexisting)

        # Re-running initialize (the forward migration) creates it.
        migrated = store_on(dsn, initialize=True)
        assert RETIREMENT_TABLE in table_names(migrated)
        assert (
            "idx_fleet_release_generation_retire_agent_gen"
            in all_index_names(migrated)
        )

        # The migrated table is usable, not merely present.
        kw = _record_kwargs()
        migrated.record_fleet_release_generation_retirement(**kw)
        row = migrated.get_fleet_release_generation_retirement(
            kw["agent_id"], kw["generation"]
        )
        assert row is not None
        assert row["terminal_state"] == "aborted"

    def test_reinitialize_is_idempotent(self) -> None:
        """Applying the schema twice neither errors nor drops existing rows."""
        schema, dsn = create_schema()
        first = store_on(dsn, initialize=True)
        kw = _record_kwargs(terminal_state="committed", disposition=None)
        first.record_fleet_release_generation_retirement(**kw)

        second = store_on(dsn, initialize=True)
        row = second.get_fleet_release_generation_retirement(
            kw["agent_id"], kw["generation"]
        )
        assert row is not None
        assert row["terminal_state"] == "committed"


# ---------------------------------------------------------------------------
# Accessor round-trip, UPSERT, newest-wins, transaction semantics
# ---------------------------------------------------------------------------


class TestAccessorRoundTrip:
    def test_record_then_read(self) -> None:
        db = ephemeral_store()
        kw = _record_kwargs()
        db.record_fleet_release_generation_retirement(**kw)
        row = db.get_fleet_release_generation_retirement(
            kw["agent_id"], kw["generation"]
        )
        assert row is not None
        assert row["agent_id"] == kw["agent_id"]
        assert row["generation"] == kw["generation"]
        assert row["epoch_id"] == kw["epoch_id"]
        assert row["terminal_state"] == "aborted"
        assert row["disposition"] == "retain_installed"
        assert row["reason"] == kw["reason"]
        assert row["prepared_at"] == kw["prepared_at"]
        assert row["retired_at"] == kw["retired_at"]
        db.close()

    def test_missing_pair_returns_none(self) -> None:
        db = ephemeral_store()
        assert db.get_fleet_release_generation_retirement("nobody", "no-gen") is None
        db.close()

    def test_blank_identifiers_return_none_without_query(self) -> None:
        db = ephemeral_store()
        assert db.get_fleet_release_generation_retirement("", "gen") is None
        assert db.get_fleet_release_generation_retirement("agent", "") is None
        db.close()

    def test_optional_fields_default_to_null(self) -> None:
        db = ephemeral_store()
        kw = _record_kwargs(
            terminal_state="committed",
            disposition=None,
            reason=None,
            prepared_at=None,
        )
        db.record_fleet_release_generation_retirement(**kw)
        row = db.get_fleet_release_generation_retirement(
            kw["agent_id"], kw["generation"]
        )
        assert row is not None
        assert row["disposition"] is None
        assert row["reason"] is None
        assert row["prepared_at"] is None
        db.close()

    def test_upsert_newest_wins_on_same_pair(self) -> None:
        db = ephemeral_store()
        first = _record_kwargs(
            terminal_state="aborted",
            epoch_id="epoch_first",
            disposition="retain_installed",
            reason="aborted first",
        )
        db.record_fleet_release_generation_retirement(**first)

        later = utcnow()
        second = dict(first)
        second.update(
            terminal_state="committed",
            epoch_id="epoch_second",
            disposition=None,
            reason="committed later",
            retired_at=later,
            updated_at=later,
        )
        db.record_fleet_release_generation_retirement(**second)

        rows = db.query_all(
            "SELECT * FROM %s WHERE agent_id = ? AND generation = ?"
            % RETIREMENT_TABLE,
            (first["agent_id"], first["generation"]),
        )
        assert len(rows) == 1

        row = db.get_fleet_release_generation_retirement(
            first["agent_id"], first["generation"]
        )
        assert row["epoch_id"] == "epoch_second"
        assert row["terminal_state"] == "committed"
        assert row["reason"] == "committed later"
        db.close()

    def test_distinct_generations_coexist(self) -> None:
        db = ephemeral_store()
        agent = "agent_shared"
        a = _record_kwargs(agent_id=agent, generation="gen-a")
        b = _record_kwargs(agent_id=agent, generation="gen-b")
        db.record_fleet_release_generation_retirement(**a)
        db.record_fleet_release_generation_retirement(**b)
        assert (
            db.get_fleet_release_generation_retirement(agent, "gen-a")["generation"]
            == "gen-a"
        )
        assert (
            db.get_fleet_release_generation_retirement(agent, "gen-b")["generation"]
            == "gen-b"
        )
        db.close()

    def test_invalid_terminal_state_rejected(self) -> None:
        db = ephemeral_store()
        with pytest.raises(ValueError):
            db.record_fleet_release_generation_retirement(
                **_record_kwargs(terminal_state="proved")
            )
        db.close()

    @pytest.mark.parametrize("missing", ["agent_id", "generation", "epoch_id"])
    def test_required_identifiers_rejected(self, missing: str) -> None:
        db = ephemeral_store()
        with pytest.raises(ValueError):
            db.record_fleet_release_generation_retirement(
                **_record_kwargs(**{missing: "  "})
            )
        db.close()


class TestTransactionSemantics:
    def test_record_and_read_inside_open_transaction(self) -> None:
        db = ephemeral_store()
        kw = _record_kwargs(terminal_state="committed")
        with db.transaction() as conn:
            db.record_fleet_release_generation_retirement(conn=conn, **kw)
            row = db.get_fleet_release_generation_retirement(
                kw["agent_id"], kw["generation"], conn=conn
            )
            assert row is not None
            assert row["terminal_state"] == "committed"
        # The committed transaction is visible on the store afterward.
        persisted = db.get_fleet_release_generation_retirement(
            kw["agent_id"], kw["generation"]
        )
        assert persisted is not None
        db.close()

    def test_rolled_back_transaction_leaves_no_row(self) -> None:
        db = ephemeral_store()
        kw = _record_kwargs()

        class _Boom(RuntimeError):
            pass

        with pytest.raises(_Boom):
            with db.transaction() as conn:
                db.record_fleet_release_generation_retirement(conn=conn, **kw)
                raise _Boom()

        assert (
            db.get_fleet_release_generation_retirement(
                kw["agent_id"], kw["generation"]
            )
            is None
        )
        db.close()


# ---------------------------------------------------------------------------
# Packaged Postgres schema / protocol surface
# ---------------------------------------------------------------------------


class TestPackagedSchemaAndProtocol:
    def test_table_and_indexes_in_schema_file(self) -> None:
        from pathlib import Path

        schema_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "mac"
            / "data"
            / "postgres"
            / "schema.sql"
        )
        text = schema_path.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS fleet_release_generation_retirements" in text
        assert "idx_fleet_release_generation_retire_agent_gen" in text
        assert "terminal_state IN ('committed', 'aborted')" in text

    def test_accessors_declared_on_protocol(self) -> None:
        from mac.store import Store

        assert hasattr(Store, "record_fleet_release_generation_retirement")
        assert hasattr(Store, "get_fleet_release_generation_retirement")

    def test_store_satisfies_protocol(self) -> None:
        from mac.store import Store

        db = ephemeral_store()
        assert isinstance(db, Store)
        db.close()
