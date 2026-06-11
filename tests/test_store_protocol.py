"""Contract tests for the Store protocol.

These tests pin down the persistence-layer interface that both SQLiteStore
and any future backend (PostgresStore, etc.) must satisfy. They use only
the Store protocol surface — no SQLite-specific behavior — so the same
test bodies will run against PostgresStore once it lands in Phase 3.2.
"""

from __future__ import annotations

import os

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


def test_store_path_attribute(store: Store) -> None:
    assert store.path == ":memory:"


def test_module_exports_store_protocol_and_error() -> None:
    import mac

    assert mac.Store is Store
    assert mac.StoreError is StoreError
