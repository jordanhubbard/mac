"""Schema-content tests for the canonical Postgres DDL.

`src/mac/data/postgres/schema.sql` is now the only schema: the SQLite
implementation it used to be checked against has been removed. These tests no
longer guard a port against drift -- there is nothing to drift from -- so they
assert the structure that the rest of the system depends on directly, plus a
floor guard so a parsing regression cannot make any of it vacuously pass.
"""

from __future__ import annotations

import re
import pathlib
from pathlib import Path

import pytest


def _schema_text() -> str:
    here = Path(__file__).resolve().parent.parent
    return (here / "src" / "mac" / "data" / "postgres" / "schema.sql").read_text()


def _create_table_names(text: str) -> set:
    """Every ``CREATE TABLE IF NOT EXISTS <name> (`` in a schema source.

    Requiring the opening paren excludes prose like "CREATE TABLE IF NOT EXISTS
    skips already-present tables" that appears in comments.
    """
    return set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(", text))


# Derived from the schema itself rather than hand-maintained: a hardcoded list
# went stale once already (asserting 64 tables while the schema had grown to 67,
# which masked the missing service_roles/service_claims). The floor guard below
# is what stops this being circular.
EXPECTED_TABLES = sorted(_create_table_names(_schema_text()))


@pytest.fixture(scope="module")
def schema_sql() -> str:
    return _schema_text()


def test_schema_file_exists_and_non_empty(schema_sql: str) -> None:
    assert len(schema_sql) > 1000


@pytest.mark.parametrize("table", EXPECTED_TABLES)
def test_each_table_is_created(schema_sql: str, table: str) -> None:
    pattern = r"CREATE TABLE IF NOT EXISTS\s+" + re.escape(table) + r"\s*\("
    assert re.search(pattern, schema_sql), f"missing CREATE TABLE for {table}"


def test_schema_table_count_is_sane() -> None:
    # A floor guard so a parsing regression that finds zero tables cannot make
    # the per-table checks vacuously pass.
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


def test_schema_enumerates_exactly_the_task_states(schema_sql: str) -> None:
    """The task-state enum is written twice: in `TaskState` and in this trigger.

    When NEEDS_INPUT was added the trigger copy was missed. Fresh databases
    built from this schema accepted the new state and CI passed, while the live
    hub -- whose trigger function predated the change -- rejected every write
    with `invalid task state`. A state the ledger can reach but the database
    refuses is a 500 on a production endpoint.
    """
    from mac.models import TaskState

    expected = {state.value for state in TaskState}

    pg_enum = re.search(
        r"IF NEW\.state NOT IN \((.*?)\) THEN", schema_sql, re.DOTALL
    )
    assert pg_enum, "postgres task-state trigger not found"
    assert set(re.findall(r"'([a-z_]+)'", pg_enum.group(1))) == expected


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
    # Additive-migration helper used by initialize().
    import inspect

    params = list(inspect.signature(PostgresStore.ensure_column).parameters)
    assert params == ["self", "table", "column", "definition"]


def test_additive_columns_are_present_in_schema(
    schema_sql: str,
) -> None:
    """Guard the columns that exposed drift during the live migration rehearsal."""
    for table, column in (
        ("fleet_release_epochs", "abort_disposition"),
        ("tasks", "human_assignees"),
        ("tasks", "created_by_human"),
        ("tasks", "idempotency_key"),
    ):
        assert re.search(
            r"ALTER TABLE %s\s+ADD COLUMN IF NOT EXISTS\s+%s"
            % (table, column),
            schema_sql,
        ), "%s.%s lacks a Postgres additive migration" % (table, column)
    assert "idx_tasks_idempotency_key" in schema_sql


def test_execution_cohort_backfill_is_versioned_and_receipt_strict(
    schema_sql: str,
) -> None:
    assert "CREATE TABLE IF NOT EXISTS telemetry_data_migrations" in schema_sql
    assert "execution_cohort_historical_backfill_v2" in schema_sql
    assert "execution_cohort_preliminary_package_repair_v3" in schema_sql
    assert "unknown_managed_mode" in schema_sql
    assert "historical_package_mode_unproven" in schema_sql
    assert "historical_synchronized_pipeline_receipt" in schema_sql
    assert "FROM work_package_publication_finalizations AS finalization" in schema_sql

    block = re.search(
        r"DO \$execution_cohort_historical_backfill_v2\$(?P<body>.*?)"
        r"\$execution_cohort_historical_backfill_v2\$;",
        schema_sql,
        re.DOTALL,
    )
    assert block, "versioned cohort backfill block is missing"
    body = block.group("body")
    assert body.index("IF NOT EXISTS") < body.index("FROM work_packages AS package")
    assert body.index("FROM work_packages AS package") < body.index(
        "FROM tasks AS task"
    )
    assert body.index("FROM tasks AS task") < body.index(
        "INSERT INTO telemetry_data_migrations"
    )

    # Existing preliminary deployments get the expanded route CHECK before the
    # v2 repair writes unknown_managed_mode; the append-only trigger is restored
    # only after the atomic repair/backfill block.
    assert schema_sql.index("$execution_cohort_route_contract$;") < schema_sql.index(
        "$execution_cohort_historical_backfill_v2$;"
    )
    trigger_position = schema_sql.index(
        "CREATE TRIGGER trg_execution_cohort_append_only",
        schema_sql.index("$execution_cohort_historical_backfill_v2$;"),
    )
    assert trigger_position > schema_sql.index(
        "$execution_cohort_historical_backfill_v2$;"
    )
    repair_position = schema_sql.index(
        "$execution_cohort_preliminary_package_repair_v3$;"
    )
    assert repair_position > schema_sql.index(
        "$execution_cohort_historical_backfill_v2$;"
    )
    assert trigger_position > repair_position


def test_postgres_telemetry_keeps_lossless_controller_and_health_records(
    schema_sql: str,
) -> None:
    assert "CREATE TABLE IF NOT EXISTS execution_cohort_configurations" in schema_sql
    assert "trg_execution_cohort_configuration_append_only" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS work_package_controller_outcomes" in schema_sql
    assert "outcome_index INTEGER NOT NULL CHECK (outcome_index >= -1)" in schema_sql
    assert "trg_work_package_controller_outcome_append_only" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS work_package_telemetry_health" in schema_sql
    assert "'controller', 'admission', 'integration', 'certification'" in schema_sql
    assert "FROM work_package_controller_outcomes" in schema_sql
