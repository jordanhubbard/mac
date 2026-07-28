import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mac.sqlite_postgres_migration import (
    REPORT_SCHEMA,
    SQLitePostgresMigrationError,
    TablePlan,
    _adapt_copy_row,
    _atomic_report,
    _canonical_value,
    _copy_table,
    _destination_columns,
    _destination_table_digest,
    _load_report,
    _migration_source_tables,
    _reset_sequences,
    _row_query,
    _sha256_file,
    _source_columns,
    _source_foreign_keys,
    _source_table_digest,
    _source_tables,
    _target_fingerprint,
    _topological_tables,
    _update_row_digest,
    _verify_destination_foreign_keys,
)


def test_migration_omits_only_proved_empty_legacy_source_tables():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE current_table (id TEXT PRIMARY KEY);
        CREATE TABLE task_lifecycle_outbox (id TEXT PRIMARY KEY);
        """
    )

    assert _migration_source_tables(conn) == (
        ["current_table"],
        ["task_lifecycle_outbox"],
    )

    conn.execute("INSERT INTO task_lifecycle_outbox VALUES ('outbox-1')")
    with pytest.raises(
        SQLitePostgresMigrationError,
        match="refusing to omit data",
    ):
        _migration_source_tables(conn)


def test_topological_tables_puts_parents_before_children():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE parent (id TEXT PRIMARY KEY);
        CREATE TABLE child (
            id TEXT PRIMARY KEY,
            parent_id TEXT NOT NULL REFERENCES parent(id)
        );
        CREATE TABLE grandchild (
            id TEXT PRIMARY KEY,
            child_id TEXT NOT NULL REFERENCES child(id)
        );
        """
    )

    order = _topological_tables(conn, ["grandchild", "parent", "child"])

    assert order.index("parent") < order.index("child")
    assert order.index("child") < order.index("grandchild")


def test_canonical_values_match_cross_backend_representations():
    assert _canonical_value(1, "boolean") == _canonical_value(True, "boolean")
    assert _canonical_value('{"z":1,"a":[true,null]}', "jsonb") == _canonical_value(
        {"a": [True, None], "z": 1}, "jsonb"
    )
    assert _canonical_value("2026-07-26T12:00:00Z", "timestamp with time zone") == _canonical_value(
        datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
        "timestamp with time zone",
    )
    assert _canonical_value(None, "text") == b"n"
    assert _canonical_value('not-json', "jsonb") == b'j"not-json"'
    assert _canonical_value("not-a-time", "timestamp") == b"tnot-a-time"
    assert _canonical_value(True, "integer") == b"i1"
    assert _canonical_value(17, "integer") == b"i17"
    assert _canonical_value(1.5, "double precision") == b"f" + (1.5).hex().encode()
    assert _canonical_value(math.nan, "double precision") == b"fnan"
    assert _canonical_value(math.inf, "double precision") == b"f+inf"
    assert _canonical_value(-math.inf, "double precision") == b"f-inf"
    assert _canonical_value(Decimal("1.20"), "numeric") == b"d1.20"
    assert _canonical_value(memoryview(b"abc"), "bytea") == b"babc"
    assert _canonical_value(["x", 1], "text").startswith(b"j")
    assert _canonical_value("plain", "text") == b"splain"


def test_atomic_report_is_owner_only_and_round_trips(tmp_path):
    path = tmp_path / "migration.json"
    report = {"schema": REPORT_SCHEMA, "status": "copying"}

    _atomic_report(path, report)

    assert json.loads(path.read_text()) == report
    assert os.stat(path).st_mode & 0o777 == 0o600
    _atomic_report(path, {**report, "status": "verified"})
    assert json.loads(path.read_text())["status"] == "verified"


def test_source_introspection_digest_and_cycle_detection(tmp_path):
    path = tmp_path / "source.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE parent (id INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE child (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES parent(id),
            enabled INTEGER NOT NULL
        );
        INSERT INTO parent VALUES (2, 'b'), (1, 'a');
        INSERT INTO child VALUES (1, 1, 1);
        """
    )
    assert _source_tables(conn) == ["child", "parent"]
    assert _source_columns(conn, "parent") == (("id", "value"), ("id",))
    plan = TablePlan(
        "parent",
        ("id", "value"),
        ("id",),
        {"id": "integer", "value": "text"},
    )
    count, digest = _source_table_digest(conn, plan)
    assert count == 2 and len(digest) == 64
    assert _sha256_file(path) == _sha256_file(path)
    row_digest = __import__("hashlib").sha256()
    _update_row_digest(row_digest, (1, "a"), ("integer", "text"))
    assert len(row_digest.hexdigest()) == 64

    cyclic = sqlite3.connect(":memory:")
    cyclic.executescript(
        """
        CREATE TABLE left_side (
            id INTEGER PRIMARY KEY,
            right_id INTEGER REFERENCES right_side(id)
        );
        CREATE TABLE right_side (
            id INTEGER PRIMARY KEY,
            left_id INTEGER REFERENCES left_side(id)
        );
        """
    )
    with pytest.raises(SQLitePostgresMigrationError, match="contains a cycle"):
        _topological_tables(cyclic, ["left_side", "right_side"])


def test_row_queries_and_copy_adaptation():
    plan = TablePlan(
        "flags",
        ("id", "enabled", "note"),
        ("id",),
        {"id": "integer", "enabled": "boolean", "note": "text"},
    )
    assert 'ORDER BY "id"' in _row_query("flags", plan.columns, plan.primary_key)
    assert 'ORDER BY "id", "enabled", "note"' in _row_query(
        "flags", plan.columns, ()
    )
    assert _adapt_copy_row((1, 1, None), plan) == (1, True, None)


class _RowsResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _DigestCursor:
    def __init__(self, rows):
        self.rows = rows
        self.itersize = 0
        self.query = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query):
        self.query = query

    def __iter__(self):
        return iter(self.rows)


class _DigestConnection:
    def __init__(self, rows):
        self.rows = rows
        self.cursor_instance = None

    def transaction(self):
        return _Transaction()

    def cursor(self, name=None):
        self.cursor_instance = _DigestCursor(self.rows)
        return self.cursor_instance


def test_destination_digest_streams_and_matches_source_semantics():
    plan = TablePlan(
        "mixed",
        ("id", "enabled", "payload", "observed_at"),
        ("id",),
        {
            "id": "integer",
            "enabled": "boolean",
            "payload": "jsonb",
            "observed_at": "timestamp with time zone",
        },
    )
    target = _DigestConnection(
        [
            (
                1,
                True,
                {"z": 1, "a": []},
                datetime(2026, 7, 26, tzinfo=timezone.utc),
            )
        ]
    )
    count, digest = _destination_table_digest(target, plan)
    assert count == 1 and len(digest) == 64
    assert target.cursor_instance.itersize == 4096


class _CopySink:
    def __init__(self):
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def write_row(self, row):
        self.rows.append(row)


class _CopyCursor:
    def __init__(self, sink):
        self.sink = sink

    def copy(self, query):
        self.query = query
        return self.sink


class _CopyTarget:
    def __init__(self):
        self.sink = _CopySink()

    def cursor(self):
        return _CopyCursor(self.sink)


def test_copy_table_batches_and_hashes_every_row():
    source = sqlite3.connect(":memory:")
    source.executescript(
        """
        CREATE TABLE flags (id INTEGER PRIMARY KEY, enabled INTEGER, note TEXT);
        INSERT INTO flags VALUES (2, 0, NULL), (1, 1, 'yes');
        """
    )
    plan = TablePlan(
        "flags",
        ("id", "enabled", "note"),
        ("id",),
        {"id": "integer", "enabled": "boolean", "note": "text"},
    )
    target = _CopyTarget()
    count, digest = _copy_table(source, target, plan, batch_size=1)
    source_count, source_digest = _source_table_digest(source, plan)
    assert (count, digest) == (source_count, source_digest)
    assert target.sink.rows == [(1, True, "yes"), (2, False, None)]


def test_destination_columns_and_target_fingerprint(monkeypatch):
    class Target:
        def execute(self, query, params):
            assert params == ("tasks",)
            return _RowsResult([("id", "text"), ("priority", "integer")])

    assert _destination_columns(Target(), "tasks") == {
        "id": "text",
        "priority": "integer",
    }
    assert len(_target_fingerprint("postgresql:///mac")) == 64

    import psycopg.conninfo

    monkeypatch.setattr(
        psycopg.conninfo,
        "conninfo_to_dict",
        lambda dsn: (_ for _ in ()).throw(ValueError("bad dsn")),
    )
    assert len(_target_fingerprint("postgresql:broken")) == 64


def test_source_and_destination_foreign_key_verification():
    source = sqlite3.connect(":memory:")
    source.executescript(
        """
        CREATE TABLE parent (a INTEGER, b INTEGER, PRIMARY KEY (a, b));
        CREATE TABLE child (
            id INTEGER PRIMARY KEY,
            a INTEGER,
            b INTEGER,
            FOREIGN KEY (a, b) REFERENCES parent(a, b)
        );
        """
    )
    relations = _source_foreign_keys(source, ["parent", "child"])
    assert relations == [
        {
            "table": "child",
            "id": 0,
            "parent": "parent",
            "child_columns": ["a", "b"],
            "parent_columns": ["a", "b"],
        }
    ]

    class Target:
        def __init__(self, count):
            self.count = count

        def execute(self, query):
            return _RowsResult([(self.count,)])

    assert _verify_destination_foreign_keys(Target(0), relations) == 1
    with pytest.raises(SQLitePostgresMigrationError, match="orphan"):
        _verify_destination_foreign_keys(Target(2), relations)
    with pytest.raises(SQLitePostgresMigrationError, match="cannot verify"):
        _verify_destination_foreign_keys(
            Target(0),
            [
                {
                    "table": "child",
                    "id": 9,
                    "parent": "parent",
                    "child_columns": ["a"],
                    "parent_columns": [],
                }
            ],
        )


def test_sequence_reset_handles_serial_and_plain_columns():
    class Target:
        def __init__(self):
            self.calls = 0
            self.setval = None

        def execute(self, query, params=None):
            self.calls += 1
            if self.calls == 1:
                return _RowsResult([("public.events_sequence_seq",)])
            if self.calls == 2:
                return _RowsResult([(8,)])
            if self.calls == 3:
                self.setval = params
                return _RowsResult([(8,)])
            return _RowsResult([(None,)])

    target = Target()
    plans = [
        TablePlan(
            "events",
            ("sequence", "id"),
            ("sequence",),
            {"sequence": "bigint", "id": "text"},
        )
    ]
    assert _reset_sequences(target, plans) == 1
    assert target.setval == ("public.events_sequence_seq", 8, True)


def test_load_report_accepts_only_valid_receipts(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"schema": REPORT_SCHEMA, "status": "copying"}))
    assert _load_report(path)["status"] == "copying"
    path.write_text("{")
    with pytest.raises(SQLitePostgresMigrationError, match="cannot read"):
        _load_report(path)
    path.write_text(json.dumps({"schema": "wrong"}))
    with pytest.raises(SQLitePostgresMigrationError, match="schema is invalid"):
        _load_report(path)
    with pytest.raises(SQLitePostgresMigrationError, match="cannot read"):
        _load_report(tmp_path / "missing.json")


def test_database_migration_cli_requires_stopped_hub(monkeypatch, tmp_path):
    from mac import cli

    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "database",
            "migrate-sqlite-to-postgres",
            "--sqlite",
            str(tmp_path / "mac.db"),
            "--postgres-url",
            "postgresql:///mac",
        ]
    )

    with pytest.raises(Exception, match="requires --hub-stopped"):
        args.func(args)
