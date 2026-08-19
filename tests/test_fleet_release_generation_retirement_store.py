"""Store-level deploy-generation retirement facts.

Covers the table/index on a fresh database, additive migration from a
pre-existing partial table, and the record / newest-lookup accessors
(including in-transaction write, UPSERT, newest-wins, and rollback).

PostgreSQL is the only backend. Tests use ``ephemeral_store()`` the way the
rest of the store suite does; that helper is the engine the fleet runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac.store_postgres import _translate_placeholders
from mac.test_support import (
    all_index_names,
    column_names,
    ephemeral_store,
    index_names,
    table_names,
)


TABLE = "fleet_release_generation_retirements"
LOOKUP_INDEX = "idx_fleet_release_generation_retirements_lookup"
EXPECTED_COLUMNS = {
    "agent_id",
    "generation",
    "epoch_id",
    "retired_state",
    "disposition",
    "reason",
    "prepared_at",
    "retired_at",
}


def _record(store, conn=None, **overrides):
    kwargs = dict(
        agent_id="agent_one",
        generation="gen-a",
        epoch_id="epoch_one",
        retired_state="aborted",
        prepared_at="2026-01-01T00:00:00+00:00",
        retired_at="2026-01-01T01:00:00+00:00",
        disposition="retain_installed",
        reason="operator abort",
        conn=conn,
    )
    kwargs.update(overrides)
    store.record_generation_retirement(**kwargs)


def test_fresh_initialize_creates_table_and_lookup_index():
    store = ephemeral_store()
    try:
        assert TABLE in table_names(store)
        assert EXPECTED_COLUMNS <= column_names(store, TABLE)
        assert LOOKUP_INDEX in index_names(store, TABLE)
        assert LOOKUP_INDEX in all_index_names(store)
    finally:
        store.close()


def test_schema_sql_declares_table_and_lookup_index():
    text = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "mac"
        / "data"
        / "postgres"
        / "schema.sql"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS %s" % TABLE in text
    assert LOOKUP_INDEX in text
    assert "PRIMARY KEY (agent_id, generation, epoch_id)" in text


def test_sql_placeholders_translate_for_postgres():
    translated = _translate_placeholders(
        "SELECT * FROM %s WHERE agent_id = ? AND generation = ?" % TABLE
    )
    assert translated == (
        "SELECT * FROM %s WHERE agent_id = %%s AND generation = %%s" % TABLE
    )


def test_migration_from_missing_table_recreates_it():
    store = ephemeral_store()
    try:
        store.execute("DROP TABLE IF EXISTS %s CASCADE" % TABLE)
        assert TABLE not in table_names(store)
        store.initialize()
        assert TABLE in table_names(store)
        assert EXPECTED_COLUMNS <= column_names(store, TABLE)
        assert LOOKUP_INDEX in index_names(store, TABLE)
    finally:
        store.close()


def test_migration_from_partial_existing_table_adds_columns_and_index():
    store = ephemeral_store()
    try:
        store.execute("DROP TABLE IF EXISTS %s CASCADE" % TABLE)
        store.execute(
            """
            CREATE TABLE %s (
                agent_id TEXT NOT NULL,
                generation TEXT NOT NULL,
                epoch_id TEXT NOT NULL,
                PRIMARY KEY (agent_id, generation, epoch_id)
            )
            """
            % TABLE
        )
        assert column_names(store, TABLE) == {
            "agent_id",
            "generation",
            "epoch_id",
        }
        store.initialize()
        assert EXPECTED_COLUMNS <= column_names(store, TABLE)
        assert LOOKUP_INDEX in index_names(store, TABLE)
        _record(store)
        row = store.newest_generation_retirement("agent_one", "gen-a")
        assert row is not None
        assert row["retired_state"] == "aborted"
    finally:
        store.close()


def test_accessor_round_trip():
    store = ephemeral_store()
    try:
        assert store.newest_generation_retirement("agent_one", "gen-a") is None
        _record(store)
        row = store.newest_generation_retirement("agent_one", "gen-a")
        assert row is not None
        assert row["agent_id"] == "agent_one"
        assert row["generation"] == "gen-a"
        assert row["epoch_id"] == "epoch_one"
        assert row["retired_state"] == "aborted"
        assert row["disposition"] == "retain_installed"
        assert row["reason"] == "operator abort"
        assert row["prepared_at"] == "2026-01-01T00:00:00+00:00"
        assert row["retired_at"] == "2026-01-01T01:00:00+00:00"
        assert store.newest_generation_retirement("agent_one", "gen-missing") is None
        assert store.newest_generation_retirement("agent_other", "gen-a") is None
    finally:
        store.close()


def test_upsert_updates_in_place_for_same_epoch():
    store = ephemeral_store()
    try:
        _record(store, retired_state="aborted", reason="first")
        _record(
            store,
            retired_state="committed",
            disposition=None,
            reason="retried as commit",
            retired_at="2026-01-01T02:00:00+00:00",
        )
        rows = store.query_all(
            "SELECT * FROM %s WHERE agent_id = ?" % TABLE, ("agent_one",)
        )
        assert len(rows) == 1
        assert rows[0]["retired_state"] == "committed"
        assert rows[0]["reason"] == "retried as commit"
        assert rows[0]["disposition"] is None
    finally:
        store.close()


def test_newest_wins_across_epochs():
    store = ephemeral_store()
    try:
        _record(
            store,
            epoch_id="epoch_old",
            retired_state="aborted",
            retired_at="2026-01-01T01:00:00+00:00",
        )
        _record(
            store,
            epoch_id="epoch_new",
            retired_state="committed",
            retired_at="2026-01-01T03:00:00+00:00",
            reason="later epoch",
        )
        row = store.newest_generation_retirement("agent_one", "gen-a")
        assert row is not None
        assert row["epoch_id"] == "epoch_new"
        assert row["retired_state"] == "committed"
        assert row["reason"] == "later epoch"
    finally:
        store.close()


def test_newest_wins_same_retired_at_uses_epoch_id():
    store = ephemeral_store()
    try:
        _record(
            store,
            epoch_id="epoch_aaa",
            retired_at="2026-01-01T01:00:00+00:00",
        )
        _record(
            store,
            epoch_id="epoch_zzz",
            retired_state="committed",
            retired_at="2026-01-01T01:00:00+00:00",
            reason="tie break",
        )
        row = store.newest_generation_retirement("agent_one", "gen-a")
        assert row is not None
        assert row["epoch_id"] == "epoch_zzz"
        assert row["reason"] == "tie break"
    finally:
        store.close()


def test_record_inside_transaction_commits_and_rollback_discards():
    store = ephemeral_store()
    try:
        with store.transaction() as conn:
            _record(store, conn=conn, epoch_id="epoch_ok")
            seen = store.newest_generation_retirement(
                "agent_one", "gen-a", conn=conn
            )
            assert seen is not None
            assert seen["epoch_id"] == "epoch_ok"
        row = store.newest_generation_retirement("agent_one", "gen-a")
        assert row is not None
        assert row["epoch_id"] == "epoch_ok"

        class _Boom(RuntimeError):
            pass

        with pytest.raises(_Boom):
            with store.transaction() as conn:
                _record(
                    store,
                    conn=conn,
                    epoch_id="epoch_bad",
                    retired_at="2026-01-01T09:00:00+00:00",
                )
                raise _Boom("rollback")
        row = store.newest_generation_retirement("agent_one", "gen-a")
        assert row is not None
        assert row["epoch_id"] == "epoch_ok"
    finally:
        store.close()


def test_invalid_retired_state_is_rejected():
    store = ephemeral_store()
    try:
        with pytest.raises(ValueError, match="aborted or committed"):
            _record(store, retired_state="open")
        with pytest.raises(ValueError, match="agent_id, generation, and epoch_id"):
            _record(store, agent_id="")
        with pytest.raises(ValueError, match="prepared_at and retired_at"):
            _record(store, prepared_at="")
        with pytest.raises(ValueError, match="prepared_at and retired_at"):
            _record(store, retired_at="   ")
        assert store.newest_generation_retirement("agent_one", "gen-a") is None
        assert store.newest_generation_retirement("", "gen-a") is None
        assert store.newest_generation_retirement("agent_one", "") is None
    finally:
        store.close()
