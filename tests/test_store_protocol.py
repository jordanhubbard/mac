"""Contract tests for the Store protocol.

These tests pin down the persistence-layer interface that both SQLiteStore
and any future backend (PostgresStore, etc.) must satisfy. They use only
the Store protocol surface — no SQLite-specific behavior — so the same
test bodies will run against PostgresStore once it lands in Phase 3.2.
"""

from __future__ import annotations

import sqlite3

import pytest

from mac.store import SQLiteStore, Store, StoreConnection, StoreError


@pytest.fixture()
def store() -> Store:
    s: Store = SQLiteStore(":memory:")
    s.execute(
        "CREATE TABLE t (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    yield s
    s.close()


def test_sqlite_store_satisfies_protocol() -> None:
    assert isinstance(SQLiteStore(":memory:"), Store)


def test_store_error_is_exception_subclass() -> None:
    assert issubclass(StoreError, Exception)


def test_execute_and_query_one(store: Store) -> None:
    store.execute("INSERT INTO t (id, payload) VALUES (?, ?)", ("a", "1"))
    row = store.query_one("SELECT id, payload FROM t WHERE id = ?", ("a",))
    assert row is not None
    assert row["id"] == "a"
    assert row["payload"] == "1"


def test_query_one_returns_none_when_missing(store: Store) -> None:
    assert store.query_one("SELECT id FROM t WHERE id = ?", ("missing",)) is None


def test_query_all_returns_list(store: Store) -> None:
    for i in range(3):
        store.execute(
            "INSERT INTO t (id, payload) VALUES (?, ?)", (f"k{i}", str(i))
        )
    rows = store.query_all("SELECT id FROM t ORDER BY id")
    assert [r["id"] for r in rows] == ["k0", "k1", "k2"]


def test_executemany_inserts_batch(store: Store) -> None:
    store.executemany(
        "INSERT INTO t (id, payload) VALUES (?, ?)",
        [("b0", "0"), ("b1", "1"), ("b2", "2")],
    )
    rows = store.query_all("SELECT COUNT(*) AS n FROM t")
    assert rows[0]["n"] == 3


def test_transaction_commits_on_clean_exit(store: Store) -> None:
    with store.transaction() as conn:
        assert isinstance(conn, StoreConnection)
        conn.execute(
            "INSERT INTO t (id, payload) VALUES (?, ?)", ("txn-ok", "x")
        )
    assert store.query_one("SELECT id FROM t WHERE id = ?", ("txn-ok",)) is not None


def test_transaction_rolls_back_on_exception(store: Store) -> None:
    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO t (id, payload) VALUES (?, ?)", ("txn-bad", "x")
            )
            raise _Boom("rollback")
    assert store.query_one("SELECT id FROM t WHERE id = ?", ("txn-bad",)) is None


def test_transaction_rolls_back_on_base_exception(store: Store) -> None:
    class _Cancelled(BaseException):
        pass

    with pytest.raises(_Cancelled):
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO t (id, payload) VALUES (?, ?)", ("cancelled", "x")
            )
            raise _Cancelled("cancelled")

    # The shared connection must be usable by the next request.
    with store.transaction() as conn:
        conn.execute("INSERT INTO t (id, payload) VALUES (?, ?)", ("next", "ok"))
    assert store.query_one("SELECT id FROM t WHERE id = ?", ("cancelled",)) is None
    assert store.query_one("SELECT id FROM t WHERE id = ?", ("next",)) is not None


def test_store_path_attribute(store: Store) -> None:
    assert store.path == ":memory:"


def test_module_exports_store_protocol_and_error() -> None:
    import mac

    assert mac.Store is Store
    assert mac.StoreError is StoreError


def test_sqlite_upgrade_adds_indexed_workflow_deadline(tmp_path) -> None:
    database = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(database)
    conn.execute(
        """
        CREATE TABLE workflow_runs (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            workflow_version INTEGER NOT NULL,
            definition_snapshot TEXT NOT NULL,
            state TEXT NOT NULL,
            current_node_key TEXT,
            current_task_id TEXT,
            input TEXT NOT NULL DEFAULT '{}',
            context TEXT NOT NULL DEFAULT '{}',
            tenant_id TEXT,
            started_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    upgraded = SQLiteStore(str(database))
    columns = {
        row["name"]
        for row in upgraded.query_all("PRAGMA table_info(workflow_runs)")
    }
    indexes = {
        row["name"]
        for row in upgraded.query_all("PRAGMA index_list(workflow_runs)")
    }
    assert "next_action_at" in columns
    assert "idx_workflow_runs_next_action" in indexes
    upgraded.close()


def test_sqlite_existing_open_skips_schema_ddl_during_active_writer(tmp_path) -> None:
    database = tmp_path / "authority.sqlite"
    owner = SQLiteStore(str(database))
    try:
        with owner.transaction() as conn:
            conn.execute(
                "INSERT INTO tasks (id, title, description, state, "
                "required_capabilities, dependencies, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("task_writer", "writer", "", "open", "[]", "[]", "{}", "now", "now"),
            )
            reader = SQLiteStore(str(database), initialize_schema=False)
            try:
                row = reader.query_one("SELECT COUNT(*) AS n FROM tasks")
                assert row is not None
                assert row["n"] == 0
            finally:
                reader.close()
    finally:
        owner.close()


def test_sqlite_existing_open_refuses_to_create_database(tmp_path) -> None:
    database = tmp_path / "missing.sqlite"

    with pytest.raises(StoreError, match="does not exist"):
        SQLiteStore(str(database), initialize_schema=False)

    assert not database.exists()
