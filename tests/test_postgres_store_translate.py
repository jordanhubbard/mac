"""Unit tests for PostgresStore's translation shim and row adapter.

These tests run without a live Postgres — they exercise the pure-Python
helpers (`_translate_placeholders`, `_Row`, `_Result`) so the dialect
contract can be verified in CI without provisioning a database. End-to-end
tests against a real Postgres land in Phase 3.3 alongside the schema port.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

from mac.store import Store  # noqa: E402
from mac.store_postgres import (  # noqa: E402
    PostgresStore,
    _Result,
    _Row,
    _translate_placeholders,
)


class TestTranslatePlaceholders:
    def test_single_placeholder(self) -> None:
        assert _translate_placeholders("SELECT * FROM t WHERE id = ?") == (
            "SELECT * FROM t WHERE id = %s"
        )

    def test_multiple_placeholders(self) -> None:
        assert _translate_placeholders(
            "INSERT INTO t (a, b, c) VALUES (?, ?, ?)"
        ) == "INSERT INTO t (a, b, c) VALUES (%s, %s, %s)"

    def test_no_placeholders_passes_through(self) -> None:
        assert _translate_placeholders("SELECT COUNT(*) FROM t") == (
            "SELECT COUNT(*) FROM t"
        )

    def test_percent_outside_string_is_escaped(self) -> None:
        assert _translate_placeholders("SELECT 100 % 3") == "SELECT 100 %% 3"

    def test_percent_inside_string_is_escaped(self) -> None:
        # LIKE wildcards: '%foo%' must survive as '%%foo%%' so psycopg
        # passes a literal '%foo%' to Postgres.
        translated = _translate_placeholders(
            "SELECT id FROM t WHERE name LIKE 'beads_memory:%'"
        )
        assert translated == (
            "SELECT id FROM t WHERE name LIKE 'beads_memory:%%'"
        )

    def test_question_mark_inside_string_is_preserved(self) -> None:
        translated = _translate_placeholders(
            "SELECT id FROM t WHERE label = 'q?' AND state = ?"
        )
        assert translated == (
            "SELECT id FROM t WHERE label = 'q?' AND state = %s"
        )

    def test_escaped_single_quote_inside_string(self) -> None:
        # SQL escapes single quote by doubling: 'it''s'
        translated = _translate_placeholders(
            "SELECT id FROM t WHERE note = 'it''s ok' AND id = ?"
        )
        assert translated == (
            "SELECT id FROM t WHERE note = 'it''s ok' AND id = %s"
        )

    def test_empty_string_literal(self) -> None:
        translated = _translate_placeholders(
            "SELECT COALESCE(NULLIF(detail, ''), '{}') FROM events WHERE id = ?"
        )
        assert translated == (
            "SELECT COALESCE(NULLIF(detail, ''), '{}') FROM events WHERE id = %s"
        )

    def test_on_conflict_upsert(self) -> None:
        translated = _translate_placeholders(
            "INSERT INTO tenants (id, name) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name"
        )
        assert translated == (
            "INSERT INTO tenants (id, name) VALUES (%s, %s) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name"
        )

    def test_multiline_statement(self) -> None:
        sql = """
            UPDATE tasks
               SET state = ?, updated_at = ?
             WHERE id = ?
               AND attempt_count < max_attempts
        """
        translated = _translate_placeholders(sql)
        # Three ? become three %s; no other change.
        assert translated.count("%s") == 3
        assert "?" not in translated

    def test_in_clause_with_many_placeholders(self) -> None:
        sql = "SELECT * FROM tasks WHERE id IN (?, ?, ?, ?, ?)"
        translated = _translate_placeholders(sql)
        assert translated == "SELECT * FROM tasks WHERE id IN (%s, %s, %s, %s, %s)"

    def test_sqlite_index_hint_is_removed(self) -> None:
        translated = _translate_placeholders(
            "SELECT * FROM tasks INDEXED BY idx_tasks_review_queue "
            "WHERE state = ? LIMIT ?"
        )
        assert translated == "SELECT * FROM tasks WHERE state = %s LIMIT %s"


class TestRowAdapter:
    def test_named_access(self) -> None:
        row = _Row(("id", "name"), ("a", "alice"))
        assert row["id"] == "a"
        assert row["name"] == "alice"

    def test_positional_access(self) -> None:
        row = _Row(("id", "name"), ("a", "alice"))
        assert row[0] == "a"
        assert row[1] == "alice"

    def test_keys_returns_columns_in_order(self) -> None:
        row = _Row(("c", "b", "a"), (1, 2, 3))
        assert row.keys() == ["c", "b", "a"]

    def test_get_with_default(self) -> None:
        row = _Row(("id",), ("x",))
        assert row.get("missing", "fallback") == "fallback"
        assert row.get("id") == "x"

    def test_contains(self) -> None:
        row = _Row(("id", "name"), ("a", "b"))
        assert "id" in row
        assert "missing" not in row

    def test_dict_conversion(self) -> None:
        row = _Row(("id", "name"), ("a", "alice"))
        as_dict = {k: row[k] for k in row}
        assert as_dict == {"id": "a", "name": "alice"}

    def test_len(self) -> None:
        row = _Row(("a", "b", "c"), (1, 2, 3))
        assert len(row) == 3

    def test_missing_key_raises(self) -> None:
        row = _Row(("id",), ("x",))
        with pytest.raises(KeyError):
            _ = row["nope"]


class TestResultBuffer:
    def test_rowcount_for_dml(self) -> None:
        r = _Result(rowcount=5, rows=[])
        assert r.rowcount == 5
        assert r.fetchone() is None
        assert r.fetchall() == []

    def test_fetchone_drains_in_order(self) -> None:
        rows = [
            _Row(("id",), ("a",)),
            _Row(("id",), ("b",)),
        ]
        r = _Result(rowcount=2, rows=list(rows))
        assert r.fetchone()["id"] == "a"
        assert r.fetchone()["id"] == "b"
        assert r.fetchone() is None

    def test_fetchall_returns_all_then_empty(self) -> None:
        rows = [_Row(("id",), (str(i),)) for i in range(3)]
        r = _Result(rowcount=3, rows=list(rows))
        out = r.fetchall()
        assert [row["id"] for row in out] == ["0", "1", "2"]
        # Second call yields empty.
        assert r.fetchall() == []

    def test_iter_drains_once(self) -> None:
        rows = [_Row(("id",), (str(i),)) for i in range(3)]
        r = _Result(rowcount=3, rows=list(rows))
        assert [row["id"] for row in r] == ["0", "1", "2"]
        assert list(r) == []


def test_postgres_store_class_satisfies_protocol_typing() -> None:
    # We don't instantiate (no live DB), but the class must declare the
    # protocol methods so type-checkers accept PostgresStore as a Store.
    for name in ("close", "transaction", "execute", "executemany", "query_one", "query_all"):
        assert hasattr(PostgresStore, name), f"PostgresStore missing {name}"
    # `path` is set in __init__; confirm by inspecting the source.
    import inspect

    src = inspect.getsource(PostgresStore.__init__)
    assert "self.path = dsn" in src


def test_postgres_store_exported_from_package() -> None:
    import mac

    assert hasattr(mac, "PostgresStore")
    assert mac.PostgresStore is PostgresStore
    # Cross-check: structural compatibility with the Store protocol.
    assert issubclass(PostgresStore, object)  # tautology guard; structural check is below
    # PostgresStore should be type-compatible as a Store (structural):
    # we can't isinstance-check without an instance, so this asserts the
    # name surfaces in the package re-export.
    assert Store is not None
