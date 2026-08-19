"""Schema-content tests for the canonical Postgres DDL.

`src/mac/data/postgres/schema.sql` is now the only schema: the SQLite
implementation it used to be checked against has been removed. These tests no
longer guard a port against drift -- there is nothing to drift from -- so they
assert the structure that the rest of the system depends on directly.

The expected-table set below is an INDEPENDENT committed declaration. It used
to be parsed out of schema.sql and then re-asserted against schema.sql, which
made 161 parametrized cases structurally incapable of failing (deleting the
whole `reviews` table left the file at 176 passed). Adding or removing a table
now requires a deliberate edit here, in both directions.

Text assertions only prove the DDL parses. The `postgres`-marked tests at the
bottom assert the shape of a database an actual `PostgresStore.initialize()`
produced, which is the only place a column that is declared but never created
shows up -- the `reviews.findings` failure mode that had to be ALTERed in by
hand on the live hub during a deploy.
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


_SQL_NON_COLUMN_LEADERS = {
    "primary", "foreign", "unique", "check", "constraint", "exclude", "like",
}


def _create_table_bodies(text: str) -> dict:
    """``{table: body}`` for every ``CREATE TABLE IF NOT EXISTS`` block."""
    out = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((?P<body>.*?)\n\);",
        text,
        re.DOTALL,
    ):
        out[match.group(1)] = match.group("body")
    return out


def _declared_column_names(body: str) -> set:
    """Column names in a CREATE TABLE body, skipping table-level constraints.

    The body is split on TOP-LEVEL commas only, so ``CHECK (x IN ('a','b'))``
    and ``NUMERIC(10, 2)`` are not mistaken for further column definitions;
    each remaining segment's first word is the column name.
    """
    # Strip `-- ...` comments FIRST: prose commas inside them are top-level as
    # far as the split below is concerned, and would each start a bogus
    # "column" named after the next English word.
    body = "\n".join(line.split("--", 1)[0] for line in body.splitlines())

    segments = []
    depth = 0
    current = ""
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            segments.append(current)
            current = ""
            continue
        current += char
    segments.append(current)

    names = set()
    for segment in segments:
        cleaned = segment.strip()
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", cleaned)
        if not match:
            continue
        name = match.group(1)
        if name.lower() in _SQL_NON_COLUMN_LEADERS:
            continue
        names.add(name)
    return names


# An INDEPENDENT, hand-maintained declaration of every table the schema must
# create -- deliberately NOT derived from schema.sql.
#
# This list used to be `sorted(_create_table_names(_schema_text()))`, i.e. parsed
# out of schema.sql and then re-asserted against schema.sql. Deleting the whole
# `CREATE TABLE IF NOT EXISTS reviews (...)` block left the suite at 176 passed:
# the expected set shrank in lockstep with the thing it was checking, and the
# ">= 60" floor guard (which the old comment claimed "stops this being circular")
# still held at 160. 161 parametrized cases that structurally could not fail.
#
# Adding or removing a table MUST now be a deliberate edit here. That is the
# point: `CREATE TABLE IF NOT EXISTS` is a no-op against an existing table and
# this repository has no migration framework, so an unnoticed schema change is
# only discovered on the live hub (see `reviews.findings`, which had to be
# ALTERed in by hand during a deploy).
EXPECTED_TABLES = [
    "action_events",
    "agent_config_flags",
    "agent_crash_occurrences",
    "agent_crash_reports",
    "agent_deploy_configs",
    "agent_events",
    "agent_lifecycle_events",
    "agent_provisioning_requests",
    "agent_roles",
    "agentbus_chunks",
    "agentbus_consumer_cursors",
    "agentbus_streams",
    "agents",
    "artifacts",
    "command_audit",
    "communication_accounts",
    "communication_identities",
    "conversation_threads",
    "deployments",
    "dispatch_mismatch_state",
    "dispatch_rounds",
    "environment_events",
    "environments",
    "eval_runs",
    "eval_set_events",
    "eval_sets",
    "evidence",
    "evidence_artifacts",
    "evidence_reuse_records",
    "fleet_agent_observations",
    "fleet_agents",
    "fleet_desired_source_idempotency",
    "fleet_desired_source_states",
    "fleet_desired_source_transitions",
    "fleet_directive_acks",
    "fleet_directive_activations",
    "fleet_directive_approvals",
    "fleet_directive_bindings",
    "fleet_directive_checks",
    "fleet_directive_macro_instances",
    "fleet_directive_versions",
    "fleet_directive_waivers",
    "fleet_directives",
    "fleet_events",
    "fleet_release_admission_episodes",
    "fleet_release_attestation_candidates",
    "fleet_release_epoch_agents",
    "fleet_release_epochs",
    "fleet_release_generation_retirements",
    "fleets",
    "gateway_identity_leases",
    "hub_authority_identity",
    "human_groups",
    "human_message_deliveries",
    "humans",
    "integration_findings",
    "integration_observations",
    "leases",
    "machines",
    "managed_task_publication_rollout",
    "memory_records",
    "merge_queue_entries",
    "merge_queue_windows",
    "messages",
    "mood_overlays",
    "nap_runs",
    "nap_schedules",
    "notifier_channels",
    "observability_events",
    "openclaw_conversation_executions",
    "openshell_agent_status",
    "openshell_policies",
    "openshell_policy_assignments",
    "openshell_policy_versions",
    "operator_notifications",
    "persona_instances",
    "personas",
    "pipeline_cursors",
    "platform_bindings",
    "project_events",
    "project_items",
    "project_repositories",
    "projects",
    "publications",
    "reconciliation_state",
    "representation_bindings",
    "reviews",
    "rollout_events",
    "rollouts",
    "runtime_environment_deltas",
    "runtime_environments",
    "runtime_runs",
    "schema_migration_receipts",
    "scientific_assignments",
    "scientific_decisions",
    "scientific_experiments",
    "scientific_observations",
    "scientific_optimizer_events",
    "scientific_optimizer_locks",
    "scientific_policies",
    "secret_access_audit",
    "secrets",
    "service_claims",
    "service_roles",
    "source_convergence_controller_leases",
    "source_convergence_nodes",
    "source_releases",
    "task_agent_transcripts",
    "task_break_glass_authorizations",
    "task_completions",
    "task_create_idempotency",
    "task_dependency_migrations",
    "task_dependency_quarantine",
    "task_edges",
    "task_flow_snapshots",
    "task_flow_spans",
    "task_groups",
    "task_history",
    "task_resource_contentions",
    "task_stranding_episodes",
    "task_transition_outbox",
    "tasks",
    "telemetry_data_migrations",
    "tenants",
    "users",
    "vector_refs",
    "worker_credential_events",
    "worker_credential_policy_state",
    "worker_credentials",
    "workflow_drafts",
    "workflow_run_history",
    "workflow_runs",
    "workflows",
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


def test_schema_declares_no_table_outside_the_manifest(schema_sql: str) -> None:
    """The other half of the gate: a table nobody declared is also a change.

    Without this, adding a table is invisible and the manifest silently rots
    behind the schema -- which is how the hand-maintained list went stale at 64
    tables while the schema had grown to 67 and masked service_roles /
    service_claims. Both directions, or neither.
    """
    actual = _create_table_names(schema_sql)
    expected = set(EXPECTED_TABLES)
    assert actual - expected == set(), (
        "schema.sql creates tables missing from EXPECTED_TABLES: %s "
        "(add them deliberately)" % sorted(actual - expected)
    )
    assert len(EXPECTED_TABLES) == len(set(EXPECTED_TABLES)), "duplicate entries"


def test_schema_table_count_is_sane() -> None:
    # A floor guard so a parsing regression that finds zero tables cannot make
    # the per-table checks vacuously pass. It is no longer load-bearing (the
    # manifest above is an independent declaration), but it costs nothing.
    assert len(EXPECTED_TABLES) >= 60


@pytest.mark.postgres
def test_live_schema_creates_exactly_the_manifest_tables(postgres_store) -> None:
    """Assert the tables a real `PostgresStore.initialize()` leaves behind.

    Text assertions against schema.sql only prove the DDL *parses* the way the
    regex expects. This proves it *executes*: a CREATE TABLE inside a DO block
    that silently no-ops, or one guarded by a condition that is false on a
    fresh database, shows up here and nowhere else.
    """
    rows = postgres_store.query_all(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
    )
    live = {r["table_name"] for r in rows}
    expected = set(EXPECTED_TABLES)
    assert expected - live == set(), (
        "declared in schema.sql but absent after initialize(): %s"
        % sorted(expected - live)
    )
    assert live - expected == set(), (
        "created by initialize() but not in EXPECTED_TABLES: %s"
        % sorted(live - expected)
    )


@pytest.mark.postgres
def test_live_schema_has_every_column_the_ddl_declares(
    postgres_store, schema_sql: str
) -> None:
    """Every column named in a CREATE TABLE block must exist on the live table.

    This is the shape of the defect that reached production: `reviews.findings`
    was declared in schema.sql but missing on the live hub and had to be ALTERed
    in by hand during a deploy. On a *fresh* database the two agree, so this test
    cannot catch the live-hub drift by itself -- what it does catch is DDL that
    the regex-based tests above accept but Postgres does not create (a column in
    a block that fails to execute, a type Postgres rejects, a trailing-comma
    parse the regex tolerates).

    The upgrade case -- a new column that reaches fresh databases and no
    existing one because nobody added a matching `ensure_column` in
    store_postgres.py::initialize -- needs a baseline-vs-current comparison and
    is filed as task_e7fe09f4.
    """
    rows = postgres_store.query_all(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema()"
    )
    live: dict = {}
    for row in rows:
        live.setdefault(row["table_name"], set()).add(row["column_name"])

    missing = []
    for table, body in _create_table_bodies(schema_sql).items():
        if table not in live:
            continue
        for column in _declared_column_names(body):
            if column not in live[table]:
                missing.append("%s.%s" % (table, column))
    assert not missing, "declared in schema.sql but not on the live table: %s" % sorted(
        missing
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
