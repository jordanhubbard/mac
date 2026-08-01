"""Contract tests for the Store protocol.

These tests pin down the persistence-layer interface that both SQLiteStore
and any future backend (PostgresStore, etc.) must satisfy. They use only
the Store protocol surface — no SQLite-specific behavior — so the same
test bodies will run against PostgresStore once it lands in Phase 3.2.
"""

from __future__ import annotations

import sqlite3
import threading

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


def test_file_backed_reader_does_not_wait_for_writer_transaction(tmp_path) -> None:
    database = tmp_path / "reader-writer.sqlite"
    store = SQLiteStore(str(database))
    store.execute("CREATE TABLE reader_probe (id TEXT PRIMARY KEY, value TEXT)")
    store.execute(
        "INSERT INTO reader_probe (id, value) VALUES (?, ?)",
        ("probe", "before"),
    )
    writer_started = threading.Event()
    release_writer = threading.Event()

    def writer() -> None:
        with store.transaction() as conn:
            conn.execute(
                "UPDATE reader_probe SET value = ? WHERE id = ?",
                ("after", "probe"),
            )
            writer_started.set()
            assert release_writer.wait(timeout=5)

    thread = threading.Thread(target=writer)
    thread.start()
    assert writer_started.wait(timeout=5)
    try:
        row = store.query_one(
            "SELECT value FROM reader_probe WHERE id = ?",
            ("probe",),
        )
        assert row["value"] == "before"
    finally:
        release_writer.set()
        thread.join(timeout=5)
        assert store.query_one(
            "SELECT value FROM reader_probe WHERE id = ?",
            ("probe",),
        )["value"] == "after"
        store.close()
    assert not thread.is_alive()


def test_observability_subject_query_has_covering_order_index(tmp_path) -> None:
    store = SQLiteStore(str(tmp_path / "observability-index.sqlite"))
    try:
        plan = store.query_all(
            "EXPLAIN QUERY PLAN "
            "SELECT sequence FROM observability_events "
            "WHERE kind = ? AND name = ? AND subject_type = ? AND subject_id = ? "
            "ORDER BY sequence DESC LIMIT ?",
            ("log", "llm.route", "task", "task_probe", 1000),
        )
        detail = " ".join(str(row["detail"]) for row in plan)
        assert "idx_observability_events_subject_sequence" in detail
        assert "USE TEMP B-TREE" not in detail
    finally:
        store.close()


def test_pipeline_cursor_roundtrip_and_default() -> None:
    store = SQLiteStore(":memory:")
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
    store = SQLiteStore(":memory:")
    try:
        with pytest.raises(ValueError):
            store.set_pipeline_cursor("", "name", "x")
        with pytest.raises(ValueError):
            store.set_pipeline_cursor("scope", "", "x")
        assert store.get_pipeline_cursor("", "name", "d") == "d"
    finally:
        store.close()


def test_pipeline_cursor_rejects_oversized_value() -> None:
    store = SQLiteStore(":memory:")
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


def test_both_backends_expose_the_same_store_surface():
    """The two backends must not drift apart again.

    Sixteen helpers lived on SQLiteStore alone while the protocol declared
    only the seven primitives, so isinstance(store, Store) passed and
    `GET /humans` returned 500 in production.
    """
    from mac.store import SQLiteStore
    from mac.store_postgres import PostgresStore

    def surface(cls):
        return {
            name
            for name in dir(cls)
            if not name.startswith("_") and callable(getattr(cls, name, None))
        }

    missing = surface(SQLiteStore) - surface(PostgresStore)
    assert not missing, "PostgresStore is missing: %s" % sorted(missing)
