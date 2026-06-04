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


# Authoritative table list pulled from SQLiteStore._initialize. Update
# both sides together when adding a new table.
EXPECTED_TABLES = [
    "tenants",
    "users",
    "personas",
    "hermes_instances",
    "platform_bindings",
    "tasks",
    "task_history",
    "task_transition_outbox",
    "evidence",
    "leases",
    "machines",
    "agents",
    "fleets",
    "fleet_agents",
    "fleet_events",
    "messages",
    "agentbus_streams",
    "agentbus_chunks",
    "observability_events",
    "operator_notifications",
    "notifier_channels",
    "command_audit",
    "agent_lifecycle_events",
    "agent_events",
    "mood_overlays",
    "nap_schedules",
    "nap_runs",
    "reviews",
    "publications",
    "secrets",
    "secret_access_audit",
    "conversation_threads",
    "memory_records",
    "vector_refs",
    "artifacts",
    "environments",
    "environment_events",
    "deployments",
    "runtime_environments",
    "runtime_environment_deltas",
    "runtime_runs",
    "projects",
    "project_events",
    "project_items",
    "beads_repositories",
    "integration_observations",
    "integration_findings",
    "rollouts",
    "rollout_events",
    "eval_sets",
    "eval_runs",
    "eval_set_events",
    "agent_roles",
    "workflows",
    "workflow_drafts",
    "workflow_runs",
    "workflow_run_history",
    "agent_provisioning_requests",
]


@pytest.fixture(scope="module")
def schema_sql() -> str:
    return _schema_text()


def test_schema_file_exists_and_non_empty(schema_sql: str) -> None:
    assert len(schema_sql) > 1000


@pytest.mark.parametrize("table", EXPECTED_TABLES)
def test_each_table_is_created(schema_sql: str, table: str) -> None:
    pattern = r"CREATE TABLE IF NOT EXISTS\s+" + re.escape(table) + r"\s*\("
    assert re.search(pattern, schema_sql), f"missing CREATE TABLE for {table}"


def test_expected_table_count_matches_sqlite() -> None:
    assert len(EXPECTED_TABLES) == 58, (
        "When a table is added to SQLiteStore._initialize, update both "
        "EXPECTED_TABLES here and src/mac/data/postgres/schema.sql."
    )


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
