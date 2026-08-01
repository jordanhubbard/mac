"""Contract tests for the Store protocol.

These tests pin down the persistence-layer interface that PostgresStore
and any future backend (PostgresStore, etc.) must satisfy. They use only
the Store protocol surface — no SQLite-specific behavior — so the same
test bodies will run against PostgresStore once it lands in Phase 3.2.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from mac.store import Store, StoreConnection, StoreError
from mac.test_support import ephemeral_store


@pytest.fixture()
def store() -> Store:
    s: Store = ephemeral_store()
    s.execute(
        "CREATE TABLE t (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    yield s
    s.close()


def test_store_satisfies_protocol() -> None:
    assert isinstance(ephemeral_store(), Store)


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
def test_observability_subject_query_has_covering_order_index() -> None:
    """The subject/sequence lookup must be an index scan, not a scan plus sort."""
    store = ephemeral_store()
    try:
        plan = " ".join(
            str(dict(row).get("QUERY PLAN", ""))
            for row in store.query_all(
                "EXPLAIN SELECT sequence FROM observability_events "
                "WHERE kind = ? AND name = ? AND subject_type = ? AND subject_id = ? "
                "ORDER BY sequence DESC LIMIT ?",
                ("log", "llm.route", "task", "task_probe", 1000),
            )
        )
        assert "idx_observability_events_subject_sequence" in plan, plan
        assert "Sort" not in plan, plan
    finally:
        store.close()


def test_pipeline_cursor_roundtrip_and_default() -> None:
    store = ephemeral_store()
    try:
        assert store.get_pipeline_cursor("scope", "name", "fallback") == "fallback"
        store.set_pipeline_cursor("scope", "name", "cursor-a")
        assert store.get_pipeline_cursor("scope", "name") == "cursor-a"
        # Upsert overwrites in place.
        store.set_pipeline_cursor("scope", "name", "cursor-b")
        assert store.get_pipeline_cursor("scope", "name") == "cursor-b"
        # JSON documents survive the roundtrip.
        store.set_pipeline_cursor("scope", "doc", {"a": 1, "b": [1, 2]})
        assert store.get_pipeline_cursor("scope", "doc") == {"a": 1, "b": [1, 2]}
    finally:
        store.close()


def test_pipeline_cursor_requires_scope_and_name() -> None:
    store = ephemeral_store()
    try:
        with pytest.raises(ValueError):
            store.set_pipeline_cursor("", "name", "x")
        with pytest.raises(ValueError):
            store.set_pipeline_cursor("scope", "", "x")
        assert store.get_pipeline_cursor("", "name", "d") == "d"
    finally:
        store.close()


def test_pipeline_cursor_rejects_oversized_value() -> None:
    store = ephemeral_store()
    try:
        oversized = "x" * (store.PIPELINE_CURSOR_MAX_BYTES + 1)
        with pytest.raises(ValueError, match="exceeds"):
            store.set_pipeline_cursor("scope", "name", oversized)
    finally:
        store.close()


def test_shared_store_helpers_use_sql_both_backends_accept():
    """Shared helpers must not reacquire SQLite-only SQL.

    store_helpers.py is compiled against both backends, so a SQLite-ism there
    is not a dialect nit -- it is a runtime error on the engine the fleet runs.
    `INSERT OR IGNORE` shipped this way and made every human-with-groups write
    fail on Postgres.
    """
    import ast
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "mac" / "store_helpers.py"
    ).read_text()
    # Only SQL string literals. Prose about a SQLite-ism is not a SQLite-ism,
    # and docstrings legitimately say things like "insert or replace a row".
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    statements = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", node.value, re.I)
    ]
    assert statements, "found no SQL to check -- the scan is broken, not clean"
    sqlite_only = {
        "INSERT OR IGNORE/REPLACE": r"INSERT\s+OR\s+(IGNORE|REPLACE)",
        "PRAGMA": r"\bPRAGMA\b",
        "sqlite_master": r"\bsqlite_master\b",
        "INDEXED BY": r"\bINDEXED\s+BY\b",
        "AUTOINCREMENT": r"\bAUTOINCREMENT\b",
    }
    found = sorted(
        {
            label
            for label, pattern in sqlite_only.items()
            for statement in statements
            if re.search(pattern, statement, re.I)
        }
    )
    assert not found, "SQLite-only SQL in shared store helpers: %s" % found


def test_the_backend_implements_everything_the_protocol_declares():
    """The protocol is the contract; the backend must satisfy all of it.

    Sixteen helpers once lived on Store alone while the protocol declared
    only the seven primitives, so isinstance(store, Store) passed and
    `GET /humans` returned 500 in production. With one backend left, the
    protocol is what stops that recurring.
    """
    from mac.store import Store
    from mac.store_postgres import PostgresStore

    declared = {
        name
        for name in dir(Store)
        if not name.startswith("_") and callable(getattr(Store, name, None))
    }
    missing = {name for name in declared if not hasattr(PostgresStore, name)}
    assert not missing, "PostgresStore is missing: %s" % sorted(missing)
    assert len(declared) >= 20, "protocol looks truncated: %d members" % len(declared)
