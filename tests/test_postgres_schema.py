"""Schema-content tests for the bundled Postgres DDL.

These tests verify that `src/mac/data/postgres/schema.sql` ports every
table, index, trigger, and view from the SQLite schema. They do NOT
exercise a live Postgres — live execution + parity tests against
SQLiteStore land in a follow-up commit once a testcontainers fixture is
in place. The goal here is to catch port drift: when a table is added to
`SQLiteStore._initialize`, the Postgres schema must add it too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


def _schema_text() -> str:
    here = Path(__file__).resolve().parent.parent
    return (here / "src" / "mac" / "data" / "postgres" / "schema.sql").read_text()


def _sqlite_schema_text() -> str:
    here = Path(__file__).resolve().parent.parent
    return (here / "src" / "mac" / "store.py").read_text()


def _create_table_names(text: str) -> set:
    """Every ``CREATE TABLE IF NOT EXISTS <name> (`` in a schema source.

    Requiring the opening paren excludes prose like "CREATE TABLE IF NOT EXISTS
    skips already-present tables" that appears in comments.
    """
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(", text))


# Authoritative table list DERIVED from the live SQLite schema (store.py) rather
# than hand-maintained, so the two schemas cannot silently drift: a table added
# to SQLiteStore automatically becomes a required table in the Postgres schema
# (see test_postgres_schema_has_every_sqlite_table). This replaces the previous
# hardcoded list + frozen count, which had itself gone stale (asserted 64 while
# SQLite had grown to 67, masking the missing service_roles/service_claims).
EXPECTED_TABLES = sorted(_create_table_names(_sqlite_schema_text()))


@pytest.fixture(scope="module")
def schema_sql() -> str:
    return _schema_text()


def test_schema_file_exists_and_non_empty(schema_sql: str) -> None:
    assert len(schema_sql) > 1000


@pytest.mark.parametrize("table", EXPECTED_TABLES)
def test_each_table_is_created(schema_sql: str, table: str) -> None:
    pattern = r"CREATE TABLE IF NOT EXISTS\s+" + re.escape(table) + r"\s*\("
    assert re.search(pattern, schema_sql), f"missing CREATE TABLE for {table}"


def test_postgres_schema_has_every_sqlite_table() -> None:
    """Live drift guard: every table in the SQLite schema must also be created
    in the Postgres schema. Computed from both sources, so it can never go stale
    the way the old hardcoded count did."""
    sqlite_tables = _create_table_names(_sqlite_schema_text())
    pg_tables = _create_table_names(_schema_text())
    missing = sqlite_tables - pg_tables
    assert not missing, (
        "Postgres schema (src/mac/data/postgres/schema.sql) is missing tables "
        "present in SQLiteStore (src/mac/store.py): %s" % sorted(missing)
    )


def test_sqlite_schema_table_count_is_sane() -> None:
    # A floor guard so a parsing regression that finds zero tables can't make the
    # drift check vacuously pass.
    assert len(EXPECTED_TABLES) >= 60


def test_json_extract_function_defined(schema_sql: str) -> None:
    assert "CREATE OR REPLACE FUNCTION json_extract(j jsonb, path text)" in schema_sql
    assert "CREATE OR REPLACE FUNCTION json_extract(j text, path text)" in schema_sql


def test_task_state_trigger_uses_plpgsql(schema_sql: str) -> None:
    # The CHECK behavior from SQLite (RAISE ABORT) becomes a PL/pgSQL
    # trigger function in Postgres.
    assert "CREATE OR REPLACE FUNCTION trg_tasks_state_enum()" in schema_sql
    assert "CREATE TRIGGER trg_tasks_state_enum_ins" in schema_sql
    assert "CREATE TRIGGER trg_tasks_state_enum_upd" in schema_sql
    assert "RAISE EXCEPTION 'invalid task state'" in schema_sql


def test_events_view_rewritten_with_jsonb_helpers(schema_sql: str) -> None:
    assert "CREATE OR REPLACE VIEW events AS" in schema_sql
    assert "jsonb_build_object" in schema_sql
    assert "jsonb_set" in schema_sql
    # ...and casts back to text so the column type matches SQLite output
    assert "::text AS detail" in schema_sql


def test_observability_sequence_uses_bigserial(schema_sql: str) -> None:
    # SQLite AUTOINCREMENT ported to BIGSERIAL.
    assert re.search(
        r"observability_events\s*\(\s*sequence\s+BIGSERIAL\s+PRIMARY KEY",
        schema_sql,
    )


def test_real_type_replaced_with_double_precision(schema_sql: str) -> None:
    # SQLite REAL has no exact Postgres equivalent; we use DOUBLE PRECISION.
    # No bare 'REAL' should slip through column definitions.
    assert not re.search(r"^\s*\w+\s+REAL[\s,]", schema_sql, re.MULTILINE), (
        "REAL columns must be ported to DOUBLE PRECISION in Postgres"
    )


def test_partial_unique_index_on_active_lease(schema_sql: str) -> None:
    # mac-x5el: at most one active lease per task. SQLite + Postgres
    # both support partial unique indexes with WHERE.
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS uniq_leases_active_per_task"
        r"\s+ON leases\s*\(task_id\)\s+WHERE status = 'active'",
        schema_sql,
    )


def test_leases_has_delegated_agent_id_column(schema_sql: str) -> None:
    """PR2c (spec §6.3, Option B): the dispatcher (lease owner)
    delegates lifecycle authorship to the role agent. The column must
    appear both in the CREATE TABLE (fresh installs) and in an
    additive ALTER TABLE IF NOT EXISTS (live deployments) since
    CREATE TABLE IF NOT EXISTS skips already-present tables.
    """
    # Column is declared in CREATE TABLE leases ( ... ).
    create_block = re.search(
        r"CREATE TABLE IF NOT EXISTS leases\s*\((?P<body>.*?)\);",
        schema_sql,
        re.DOTALL,
    )
    assert create_block, "leases CREATE TABLE not found"
    assert "delegated_agent_id" in create_block.group("body")
    # Additive ALTER ensures pre-existing tables also pick it up.
    assert re.search(
        r"ALTER TABLE leases\s+ADD COLUMN IF NOT EXISTS\s+delegated_agent_id",
        schema_sql,
    )


def test_reconciliation_and_workflow_deadline_schema(schema_sql: str) -> None:
    assert "CREATE TABLE IF NOT EXISTS reconciliation_state" in schema_sql
    assert "idx_reconciliation_state_lease" in schema_sql
    assert re.search(
        r"ALTER TABLE workflow_runs\s+ADD COLUMN IF NOT EXISTS\s+next_action_at",
        schema_sql,
    )
    assert "idx_workflow_runs_next_action" in schema_sql
    assert "idx_leases_status_expiry" in schema_sql
    assert "idx_tasks_state_updated" in schema_sql
    assert "idx_tasks_review_queue" in schema_sql


def test_packaged_loader_reads_schema() -> None:
    psycopg = pytest.importorskip("psycopg")  # noqa: F841
    from mac.store_postgres import _load_packaged_schema

    text = _load_packaged_schema()
    assert "CREATE OR REPLACE VIEW events" in text
    assert "json_extract" in text


def test_postgres_store_exposes_initialize_and_ensure_column() -> None:
    pytest.importorskip("psycopg")
    from mac.store_postgres import PostgresStore

    assert callable(PostgresStore.initialize)
    assert callable(PostgresStore.ensure_column)
    # Signature parity with SQLiteStore._ensure_column.
    import inspect

    params = list(inspect.signature(PostgresStore.ensure_column).parameters)
    assert params == ["self", "table", "column", "definition"]
