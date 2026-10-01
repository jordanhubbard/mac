"""Ordered, checksummed PostgreSQL schema migration authority.

The older ``schema_migration_receipts`` and ``telemetry_data_migrations``
tables record component-specific one-time work.  They are intentionally not
reused here: this module owns the database-level version and complete ordered
schema history required by ADR 0021/0027.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Iterable, Sequence

from mac.store import StoreError


POSTGRES_DATA_PATH = Path(__file__).resolve().parent / "data" / "postgres"
SCHEMA_PATH = POSTGRES_DATA_PATH / "schema.sql"
MIGRATION_PATH = POSTGRES_DATA_PATH / "migrations"
AUTHORITY_TABLES = frozenset({"schema_version", "schema_migrations"})
LEGACY_PRUNABLE_TABLES = frozenset(
    {
        "evidence_attempt_links",
        "evidence_attempt_verifications",
        "execution_cohort_assignments",
        "execution_cohort_configurations",
        "work_package_assignment_audit",
        "work_package_batch_inputs",
        "work_package_certification_jobs",
        "work_package_certifications",
        "work_package_controller_outcomes",
        "work_package_controller_station_receipts",
        "work_package_epochs",
        "work_package_finalization_outcomes",
        "work_package_history",
        "work_package_integration_batches",
        "work_package_landing_attempts",
        "work_package_landing_intents",
        "work_package_landing_receipts",
        "work_package_landing_streams",
        "work_package_lease_expiry_repairs",
        "work_package_node_candidates",
        "work_package_node_lineage",
        "work_package_plan_versions",
        "work_package_publication_finalizations",
        "work_package_ref_retirement_attempts",
        "work_package_ref_retirement_intents",
        "work_package_ref_retirement_receipts",
        "work_package_station_attempts",
        "work_package_task_links",
        "work_package_telemetry_health",
        "work_package_wip_tokens",
        "work_packages",
    }
)
# Trigger functions that lived on surviving tables (notably `tasks`) and only
# *queried* the prunable relations. DROP TABLE ... CASCADE does not remove
# them. The 2026-08-27 fleet outage was every claim failing with
# `relation "work_package_assignment_audit" does not exist`.
LEGACY_PRUNABLE_FUNCTIONS = frozenset(
    {
        "trg_evidence_attempt_links_immutable",
        "trg_evidence_attempt_package_identity",
        "trg_evidence_attempt_verification_identity",
        "trg_evidence_attempt_verifications_immutable",
        "trg_execution_cohort_append_only",
        "trg_execution_cohort_configuration_append_only",
        "trg_work_package_assignment_immutable",
        "trg_work_package_batch_fence_monotonic",
        "trg_work_package_batch_initial_state",
        "trg_work_package_batch_inputs_open",
        "trg_work_package_batch_invariants",
        "trg_work_package_batch_repository_matches",
        "trg_work_package_certification_job_lifecycle",
        "trg_work_package_certification_lifecycle",
        "trg_work_package_controller_outcome_append_only",
        "trg_work_package_controller_station_lifecycle",
        "trg_work_package_current_epoch_status",
        "trg_work_package_epochs_lifecycle",
        "trg_work_package_expiry_node_guard",
        "trg_work_package_expiry_repair_authority",
        "trg_work_package_expiry_repairs_immutable",
        "trg_work_package_expiry_task_detach_guard",
        "trg_work_package_finalization_outcome_append_only",
        "trg_work_package_history_immutable",
        "trg_work_package_landing_attempt_lifecycle",
        "trg_work_package_landing_intent_lifecycle",
        "trg_work_package_landing_receipt_lifecycle",
        "trg_work_package_landing_stream_invariants",
        "trg_work_package_lineage_carry_forward_evidence",
        "trg_work_package_node_candidate_lifecycle",
        "trg_work_package_node_lineage_immutable",
        "trg_work_package_plan_versions_immutable",
        "trg_work_package_publication_finalization_lifecycle",
        "trg_work_package_ref_retirement_append_only",
        "trg_work_package_station_attempt_append_only",
        "trg_work_package_task_claim_authority",
        "trg_work_package_task_link_candidate_state",
        "trg_work_package_task_link_executable_insert",
        "trg_work_package_task_links_identity_immutable",
        "trg_work_package_task_links_lifecycle",
        "trg_work_package_wip_lifecycle",
        "trg_work_packages_current_epoch_coherent",
        "trg_work_packages_initial_state",
        "trg_work_packages_state_transition",
    }
)
LEGACY_PRUNABLE_TASK_TRIGGERS = frozenset(
    {
        "trg_work_package_expiry_task_detach_guard",
        "trg_work_package_task_claim_authority",
    }
)
FORMER_STARTUP_ENSURE_COLUMNS = frozenset(
    {
        ("agents", "installed_packages"),
        ("agents", "attestation_key_prev_ciphertext"),
        ("agents", "attestation_key_history_ciphertext"),
        ("tasks", "human_assignees"),
        ("tasks", "created_by_human"),
        ("tasks", "idempotency_key"),
        ("agents", "dispatch_hold"),
        ("agents", "dispatch_hold_reason"),
        ("agents", "dispatch_hold_at"),
        ("agents", "consecutive_lease_expiries_no_telemetry"),
        ("agents", "last_control_stream_published_at"),
        ("agents", "last_control_stream_consumed_at"),
        ("reviews", "findings"),
    }
)
AUTHORITY_DDL = """
CREATE TABLE schema_migrations (
    ordinal INTEGER PRIMARY KEY CHECK (ordinal > 0),
    migration_id TEXT NOT NULL UNIQUE,
    checksum_sha256 CHAR(64) NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied_by TEXT NOT NULL,
    postcondition JSONB NOT NULL
);
CREATE TABLE schema_version (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    ordinal INTEGER NOT NULL,
    migration_id TEXT NOT NULL,
    checksum_sha256 CHAR(64) NOT NULL CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (singleton)
);
CREATE OR REPLACE FUNCTION trg_schema_version_consistent()
RETURNS trigger AS $$
DECLARE
    latest schema_migrations%ROWTYPE;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'schema version cannot be deleted';
    END IF;
    SELECT * INTO latest FROM schema_migrations ORDER BY ordinal DESC LIMIT 1;
    IF latest.ordinal IS NULL
       OR NEW.ordinal <> latest.ordinal
       OR NEW.migration_id <> latest.migration_id
       OR NEW.checksum_sha256 <> latest.checksum_sha256 THEN
        RAISE EXCEPTION 'schema version must match the latest migration ledger entry';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_schema_version_consistent
    BEFORE INSERT OR UPDATE OR DELETE ON schema_version
    FOR EACH ROW EXECUTE FUNCTION trg_schema_version_consistent();
CREATE OR REPLACE FUNCTION trg_schema_migrations_append_only()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'schema migrations are immutable';
    END IF;
    RAISE EXCEPTION 'schema migrations are append-only';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_schema_migrations_append_only
    BEFORE UPDATE OR DELETE ON schema_migrations
    FOR EACH ROW EXECUTE FUNCTION trg_schema_migrations_append_only();
"""


@dataclass(frozen=True)
class Migration:
    """One immutable migration in the binary's ordered migration chain."""

    migration_id: str
    sql: str
    postcondition_sql: str | None = None

    @property
    def checksum_sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def _load_sql(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - wheel packaging guard
        raise StoreError("packaged PostgreSQL migration is missing: %s" % path) from exc


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        "0001_postgresql_authority_baseline",
        _load_sql(MIGRATION_PATH / "0001_postgresql_authority_baseline.sql"),
    ),
    Migration(
        "0002_dream_candidate_store",
        _load_sql(MIGRATION_PATH / "0002_dream_candidate_store.sql"),
        # 0004 drops these tables again. Every applied postcondition is
        # re-proved on each verification, so this one holds either while the
        # tables exist or once the migration that removed them is recorded.
        """
        SELECT (
                   to_regclass(current_schema() || '.dream_runs') IS NOT NULL
               AND to_regclass(current_schema() || '.dream_candidate_entries') IS NOT NULL
               )
            OR EXISTS (
                   SELECT 1 FROM schema_migrations
                   WHERE migration_id = '0004_drop_removed_feature_tables'
               )
        """,
    ),
    Migration(
        "0003_drop_leftover_work_package_triggers",
        _load_sql(MIGRATION_PATH / "0003_drop_leftover_work_package_triggers.sql"),
        """
        SELECT to_regprocedure(
                   current_schema() || '.trg_work_package_task_claim_authority()'
               ) IS NULL
           AND to_regprocedure(
                   current_schema() || '.trg_work_package_expiry_task_detach_guard()'
               ) IS NULL
        """,
    ),
    Migration(
        "0004_drop_removed_feature_tables",
        _load_sql(MIGRATION_PATH / "0004_drop_removed_feature_tables.sql"),
        """
        SELECT bool_and(to_regclass(current_schema() || '.' || name) IS NULL)
        FROM unnest(ARRAY[
            'scientific_decisions',
            'scientific_observations',
            'scientific_assignments',
            'scientific_experiments',
            'scientific_policies',
            'scientific_optimizer_events',
            'scientific_optimizer_locks',
            'dream_candidate_entries',
            'dream_runs',
            'nap_runs',
            'nap_schedules'
        ]) AS name
        """,
    ),
    Migration(
        "0005_drop_native_merge_queue_tables",
        _load_sql(MIGRATION_PATH / "0005_drop_native_merge_queue_tables.sql"),
        """
        SELECT to_regclass(current_schema() || '.merge_queue_entries') IS NULL
           AND to_regclass(current_schema() || '.merge_queue_windows') IS NULL
        """,
    ),
    Migration(
        "0006_drop_rollout_and_deploy_tables",
        _load_sql(MIGRATION_PATH / "0006_drop_rollout_and_deploy_tables.sql"),
        """
        SELECT bool_and(to_regclass(current_schema() || '.' || name) IS NULL)
        FROM unnest(ARRAY[
            'rollout_events',
            'rollouts',
            'deployments',
            'environment_events',
            'managed_task_publication_rollout'
        ]) AS name
        """,
    ),
    Migration(
        "0007_drop_agent_provisioning_requests",
        _load_sql(MIGRATION_PATH / "0007_drop_agent_provisioning_requests.sql"),
        """
        SELECT to_regclass(current_schema() || '.agent_provisioning_requests') IS NULL
        """,
    ),
    Migration(
        "0008_drop_self_upgrade_and_release_epoch_tables",
        _load_sql(MIGRATION_PATH / "0008_drop_self_upgrade_and_release_epoch_tables.sql"),
        """
        SELECT bool_and(to_regclass(current_schema() || '.' || name) IS NULL)
           AND to_regprocedure(current_schema() || '._trg_source_releases_sha_immutable()')
               IS NULL
           AND to_regprocedure(current_schema() || '._trg_fleet_desired_source_gen_monotonic()')
               IS NULL
        FROM unnest(ARRAY[
            'fleet_upgrade_events',
            'fleet_upgrades',
            'source_convergence_nodes',
            'source_convergence_controller_leases',
            'fleet_release_attestation_candidates',
            'fleet_release_epoch_agents',
            'fleet_release_epochs',
            'fleet_release_admission_episodes',
            'fleet_desired_source_idempotency',
            'fleet_desired_source_transitions',
            'fleet_desired_source_states',
            'source_releases',
            'environments'
        ]) AS name
        """,
    ),
    Migration(
        "0009_slim_worker_credentials",
        _load_sql(MIGRATION_PATH / "0009_slim_worker_credentials.sql"),
        """
        SELECT to_regclass(current_schema() || '.worker_credential_events') IS NULL
           AND to_regclass(current_schema() || '.worker_credential_policy_state') IS NULL
           AND NOT EXISTS (
                   SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema()
                     AND table_name = 'worker_credentials'
                     AND column_name IN (
                         'fleet',
                         'environment',
                         'expected_source_commit',
                         'expected_runtime_digest',
                         'required_capabilities',
                         'package_capable',
                         'destination'
                     )
               )
        """,
    ),
)


def render_bootstrap_schema(migrations: Sequence[Migration] = MIGRATIONS) -> str:
    """Render the current bootstrap artifact from immutable ordered migrations."""

    return "".join(
        migration.sql if migration.sql.endswith("\n") else migration.sql + "\n"
        for migration in migrations
    )


def _table_bodies(sql: str) -> dict[str, str]:
    return {
        match.group(1): match.group("body")
        for match in re.finditer(
            r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\((?P<body>.*?)\n?\)(?:;|$)",
            sql,
            re.DOTALL,
        )
    }


def _column_names(body: str) -> set[str]:
    body = "\n".join(line.split("--", 1)[0] for line in body.splitlines())
    segments: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
    segments.append("".join(current))
    ignored = {"primary", "foreign", "unique", "check", "constraint", "exclude", "like"}
    columns: set[str] = set()
    for segment in segments:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", segment)
        if match and match.group(1).lower() not in ignored:
            columns.add(match.group(1))
    return columns


def _dropped_tables(sql: str) -> set[str]:
    """Tables a later statement drops and nothing after it creates again."""

    final: dict[str, bool] = {}
    for match in re.finditer(
        r"\b(?:(?P<create>CREATE TABLE(?: IF NOT EXISTS)?)|DROP TABLE(?: IF EXISTS)?)\s+(?P<name>\w+)",
        sql,
        re.IGNORECASE,
    ):
        final[match.group("name")] = match.group("create") is None
    return {table for table, dropped in final.items() if dropped}


def _dropped_functions(sql: str) -> set[str]:
    """Functions a later statement drops and nothing after it creates again.

    Unlike a trigger, a function does not go with the table it served, so a
    migration that retires one drops it explicitly.
    """

    final: dict[str, bool] = {}
    for match in re.finditer(
        r"\b(?:(?P<create>CREATE OR REPLACE FUNCTION)|DROP FUNCTION(?: IF EXISTS)?)\s+(?P<name>\w+)\s*\(",
        sql,
        re.IGNORECASE,
    ):
        final[match.group("name")] = match.group("create") is None
    return {function for function, dropped in final.items() if dropped}


def _expected_inventory(sql: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    dropped = _dropped_tables(sql)
    tables = {
        table: _column_names(body)
        for table, body in _table_bodies(sql).items()
        if table not in dropped
    }
    # Column changes apply in file order, so a column a later migration drops
    # is not expected to exist.
    for match in re.finditer(
        r"ALTER TABLE\s+(\w+)\s+(ADD|DROP) COLUMN IF (?:NOT )?EXISTS\s+(\w+)",
        sql,
        re.IGNORECASE,
    ):
        table, action, column = match.groups()
        if table in tables:
            if action.upper() == "ADD":
                tables[table].add(column)
            else:
                tables[table].discard(column)
    # An index or trigger goes with its table, so one declared on a table a
    # later migration dropped is not expected to exist.
    objects = {
        "indexes": {
            match.group(1)
            for match in re.finditer(
                r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS\s+(\w+)\s+ON\s+(\w+)", sql
            )
            if match.group(2) not in dropped
        },
        "triggers": {
            match.group(1)
            for match in re.finditer(r"CREATE TRIGGER\s+(\w+)\s.*?\bON\s+(\w+)", sql, re.DOTALL)
            if match.group(2) not in dropped
        },
        "views": set(re.findall(r"CREATE OR REPLACE VIEW\s+(\w+)", sql)),
        "functions": set(re.findall(r"CREATE OR REPLACE FUNCTION\s+(\w+)\s*\(", sql, re.IGNORECASE))
        - _dropped_functions(sql),
    }
    return tables, objects


def _later_migration_tables(migrations: Sequence[Migration]) -> set[str]:
    """Tables an unversioned install may already have from legacy lazy DDL."""

    tables: set[str] = set()
    for migration in migrations[1:]:
        tables.update(_table_bodies(migration.sql))
    return tables


def _drop_legacy_prunable_functions(cur: Any) -> None:
    """Drop leftover work-package trigger functions on surviving tables.

    ``DROP TABLE ... CASCADE`` removes triggers that live ON the dropped
    table. It does not remove triggers on ``tasks`` whose function body
    still queries the dropped table, and those leftovers abort every
    ``claim_task`` with a missing-relation error.
    """

    for trigger_name in sorted(LEGACY_PRUNABLE_TASK_TRIGGERS):
        cur.execute("DROP TRIGGER IF EXISTS %s ON tasks" % trigger_name)
    for function_name in sorted(LEGACY_PRUNABLE_FUNCTIONS):
        cur.execute("DROP FUNCTION IF EXISTS %s() CASCADE" % function_name)


def _legacy_prune_plan(cur: Any, relations: Iterable[str]) -> dict[str, Any]:
    """Return exact, read-only counts for reviewed pre-baseline legacy tables."""

    table_names = sorted(LEGACY_PRUNABLE_TABLES & set(relations))
    tables: list[dict[str, Any]] = []
    for table_name in table_names:
        cur.execute('SELECT COUNT(*) FROM "%s"' % table_name)
        row = cur.fetchone()
        tables.append({"table": table_name, "row_count": int(row[0])})
    return {
        "required": bool(tables),
        "requires_backup": bool(tables),
        "requires_authorization": bool(tables),
        "table_names": table_names,
        "tables": tables,
        "total_rows": sum(item["row_count"] for item in tables),
    }


def _prove_unversioned_baseline(
    cur: Any,
    migrations: Sequence[Migration],
    relations: Iterable[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prove baseline shape while admitting only reviewed legacy/future tables."""

    allowed_extras = _later_migration_tables(migrations) | LEGACY_PRUNABLE_TABLES
    proof = _prove_baseline(
        cur,
        migrations[0].sql,
        allowed_extra_tables=allowed_extras,
    )
    return proof, _legacy_prune_plan(cur, relations)


def _rows(cur: Any, query: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    cur.execute(query, tuple(params) if params else None)
    return list(cur.fetchall())


def _relation_names(cur: Any) -> set[str]:
    return {
        row[0]
        for row in _rows(
            cur,
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
            """,
        )
    }


def _authority_presence(cur: Any) -> set[str]:
    return AUTHORITY_TABLES & _relation_names(cur)


def _prove_baseline(
    cur: Any,
    baseline_sql: str,
    *,
    exact: bool = True,
    allowed_extra_tables: Iterable[str] = (),
) -> dict[str, Any]:
    """Prove a schema has the known baseline shape without modifying it."""

    expected_tables, expected_objects = _expected_inventory(baseline_sql)
    actual_tables = {
        row[0]
        for row in _rows(
            cur,
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
            """,
        )
    } - AUTHORITY_TABLES
    expected_names = set(expected_tables)
    missing_tables = sorted(expected_names - actual_tables)
    extra_tables = (
        sorted(actual_tables - expected_names - set(allowed_extra_tables)) if exact else []
    )

    actual_columns: dict[str, set[str]] = {}
    for table, column in _rows(
        cur,
        """
        SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema = current_schema()
        """,
    ):
        actual_columns.setdefault(table, set()).add(column)
    missing_columns: list[str] = []
    extra_columns: list[str] = []
    for table, columns in expected_tables.items():
        actual = actual_columns.get(table, set())
        missing_columns.extend("%s.%s" % (table, col) for col in sorted(columns - actual))
        if exact:
            extra_columns.extend("%s.%s" % (table, col) for col in sorted(actual - columns))

    actual_objects = {
        "indexes": {
            row[0]
            for row in _rows(
                cur,
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()",
            )
        },
        "triggers": {
            row[0]
            for row in _rows(
                cur,
                """
                SELECT t.tgname FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema() AND NOT t.tgisinternal
                """,
            )
        },
        "views": {
            row[0]
            for row in _rows(
                cur,
                "SELECT table_name FROM information_schema.views WHERE table_schema=current_schema()",
            )
        },
        "functions": {
            row[0]
            for row in _rows(
                cur,
                """
                SELECT p.proname FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname = current_schema()
                """,
            )
        },
    }
    missing_objects = {
        kind: sorted(names - actual_objects[kind])
        for kind, names in expected_objects.items()
        if names - actual_objects[kind]
    }
    problems = {
        "missing_tables": missing_tables,
        "extra_tables": extra_tables,
        "missing_columns": missing_columns,
        "extra_columns": extra_columns,
        "missing_objects": missing_objects,
    }
    if any(problems.values()):
        detail = "; ".join("%s=%s" % item for item in problems.items() if item[1])
        raise StoreError("refusing to baseline partial or unknown PostgreSQL schema: %s" % detail)
    return {
        "tables": len(expected_tables),
        "columns": sum(len(columns) for columns in expected_tables.values()),
        "indexes": len(expected_objects["indexes"]),
        "triggers": len(expected_objects["triggers"]),
        "views": len(expected_objects["views"]),
        "functions": len(expected_objects["functions"]),
    }


def _validate_chain(migrations: Sequence[Migration]) -> None:
    if not migrations:
        raise StoreError("binary contains no PostgreSQL schema migrations")
    ids = [migration.migration_id for migration in migrations]
    if len(ids) != len(set(ids)):
        raise StoreError("binary contains duplicate PostgreSQL migration IDs")
    if any(not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+", migration_id) for migration_id in ids):
        raise StoreError("PostgreSQL migration IDs must use NNNN_stable_name format")
    prefixes = [int(migration_id.split("_", 1)[0]) for migration_id in ids]
    if prefixes != sorted(prefixes) or len(prefixes) != len(set(prefixes)):
        raise StoreError("binary PostgreSQL migrations are missing or out of order")


def _inspect_versioned(
    cur: Any,
    migrations: Sequence[Migration],
    *,
    allow_behind: bool,
) -> int:
    ledger = _rows(
        cur,
        """
        SELECT ordinal, migration_id, checksum_sha256
        FROM schema_migrations ORDER BY ordinal
        """,
    )
    if not ledger:
        raise StoreError("PostgreSQL schema authority exists but its migration ledger is empty")
    for position, (ordinal, migration_id, checksum) in enumerate(ledger, start=1):
        if ordinal != position:
            raise StoreError(
                "PostgreSQL migration ledger is missing or out of order at ordinal %d" % position
            )
        if position > len(migrations):
            raise StoreError(
                "database schema is newer than this binary: %s at ordinal %d"
                % (migration_id, position)
            )
        expected = migrations[position - 1]
        if migration_id != expected.migration_id:
            known_later = migration_id in {
                migration.migration_id for migration in migrations[position:]
            }
            reason = "out of order" if known_later else "unknown/newer than this binary"
            raise StoreError(
                "PostgreSQL migration ledger is %s at ordinal %d: database=%s binary=%s"
                % (reason, position, migration_id, expected.migration_id)
            )
        if checksum != expected.checksum_sha256:
            raise StoreError(
                "PostgreSQL migration checksum drift for %s: database=%s binary=%s"
                % (migration_id, checksum, expected.checksum_sha256)
            )
    versions = _rows(
        cur,
        "SELECT ordinal, migration_id, checksum_sha256 FROM schema_version WHERE singleton",
    )
    if len(versions) != 1:
        raise StoreError("PostgreSQL schema_version must contain exactly one current-version row")
    if tuple(versions[0]) != tuple(ledger[-1]):
        raise StoreError("PostgreSQL current schema version does not match the migration ledger")
    current = len(ledger)
    if current < len(migrations) and not allow_behind:
        missing = ", ".join(migration.migration_id for migration in migrations[current:])
        raise StoreError(
            "database schema is behind this binary; explicit migration required: %s" % missing
        )
    return current


def _prove_applied_chain(
    cur: Any,
    migrations: Sequence[Migration],
    current: int,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "baseline": _prove_baseline(
            cur,
            render_bootstrap_schema(migrations[:current]),
        )
    }
    postconditions: list[str] = []
    for migration in migrations[1:current]:
        if not migration.postcondition_sql:
            raise StoreError(
                "applied migration %s has no executable postcondition" % migration.migration_id
            )
        cur.execute(migration.postcondition_sql)
        row = cur.fetchone()
        if row is None or row[0] is not True:
            raise StoreError(
                "postcondition no longer holds for PostgreSQL migration %s" % migration.migration_id
            )
        postconditions.append(migration.migration_id)
    proof["postconditions"] = postconditions
    return proof


def migration_status(
    conn: Any,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> dict[str, Any]:
    """Read-only deploy preflight; prove whether backup/migration is needed."""

    _validate_chain(migrations)
    try:
        with conn.cursor() as cur:
            presence = _authority_presence(cur)
            relations = _relation_names(cur)
            if not presence:
                if not relations:
                    return {
                        "status": "pending",
                        "database_state": "fresh",
                        "current_version": None,
                        "pending": [migration.migration_id for migration in migrations],
                        "requires_backup": True,
                        "requires_existing_baseline_authority": False,
                        "requires_legacy_schema_prune_authority": False,
                        "legacy_schema_prune": _legacy_prune_plan(cur, ()),
                    }
                proof, legacy_prune = _prove_unversioned_baseline(
                    cur,
                    migrations,
                    relations,
                )
                return {
                    "status": "pending",
                    "database_state": "existing-unversioned",
                    "current_version": None,
                    "pending": [migration.migration_id for migration in migrations],
                    "requires_backup": True,
                    "requires_existing_baseline_authority": True,
                    "requires_legacy_schema_prune_authority": legacy_prune["required"],
                    "legacy_schema_prune": legacy_prune,
                    "proof": proof,
                }
            if presence != AUTHORITY_TABLES:
                raise StoreError("PostgreSQL schema migration authority is partial or corrupt")
            current = _inspect_versioned(cur, migrations, allow_behind=True)
            proof = _prove_applied_chain(cur, migrations, current)
            pending = [migration.migration_id for migration in migrations[current:]]
            return {
                "status": "pending" if pending else "current",
                "database_state": "versioned",
                "current_version": migrations[current - 1].migration_id,
                "pending": pending,
                "requires_backup": bool(pending),
                "requires_existing_baseline_authority": False,
                "requires_legacy_schema_prune_authority": False,
                "legacy_schema_prune": _legacy_prune_plan(cur, ()),
                "proof": proof,
            }
    except StoreError:
        raise
    except Exception as exc:
        raise StoreError("PostgreSQL schema migration preflight failed: %s" % exc) from exc


def verify_schema(conn: Any, migrations: Sequence[Migration] = MIGRATIONS) -> dict[str, Any]:
    """Verify the database exactly matches the binary, performing no DDL."""

    _validate_chain(migrations)
    try:
        with conn.cursor() as cur:
            presence = _authority_presence(cur)
            if not presence:
                relations = _relation_names(cur)
                state = "fresh/uninitialized" if not relations else "existing unversioned"
                raise StoreError(
                    "PostgreSQL schema is %s; run mac-schema-migrate explicitly" % state
                )
            if presence != AUTHORITY_TABLES:
                raise StoreError("PostgreSQL schema migration authority is partial or corrupt")
            current = _inspect_versioned(cur, migrations, allow_behind=False)
            proof = _prove_applied_chain(cur, migrations, current)
            return {
                "status": "verified",
                "current_version": migrations[current - 1].migration_id,
                "ordinal": current,
                "proof": proof,
            }
    except StoreError:
        raise
    except Exception as exc:
        raise StoreError("PostgreSQL schema verification failed: %s" % exc) from exc


def apply_migrations(
    conn: Any,
    *,
    applied_by: str,
    authorize_existing_baseline: bool = False,
    authorize_legacy_schema_prune: bool = False,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> dict[str, Any]:
    """Apply pending migrations atomically after explicit deploy authorization."""

    _validate_chain(migrations)
    actor = applied_by.strip()
    if not actor:
        raise StoreError("schema migration application requires a non-empty applied_by identity")
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('mac.schema_migrations'))")
                relations_before = _relation_names(cur)
                presence = AUTHORITY_TABLES & relations_before
                if presence and presence != AUTHORITY_TABLES:
                    raise StoreError("PostgreSQL schema migration authority is partial or corrupt")

                legacy_prune = _legacy_prune_plan(cur, ())
                if not presence:
                    user_relations = relations_before - AUTHORITY_TABLES
                    if user_relations:
                        if not authorize_existing_baseline:
                            raise StoreError(
                                "existing unversioned PostgreSQL schema requires explicit "
                                "--authorize-existing-baseline after backup and quiesce"
                            )
                        baseline_proof, legacy_prune = _prove_unversioned_baseline(
                            cur,
                            migrations,
                            user_relations,
                        )
                        if legacy_prune["required"]:
                            if not authorize_legacy_schema_prune:
                                raise StoreError(
                                    "reviewed legacy PostgreSQL tables require separate explicit "
                                    "--authorize-legacy-schema-prune after backup and quiesce"
                                )
                            quoted = ", ".join(
                                '"%s"' % table_name for table_name in legacy_prune["table_names"]
                            )
                            cur.execute("LOCK TABLE %s IN ACCESS EXCLUSIVE MODE" % quoted)
                            locked_plan = _legacy_prune_plan(
                                cur,
                                legacy_prune["table_names"],
                            )
                            if locked_plan["table_names"] != legacy_prune["table_names"]:
                                raise StoreError(
                                    "legacy PostgreSQL prune plan changed while acquiring locks"
                                )
                            legacy_prune = locked_plan
                            cur.execute("DROP TABLE %s CASCADE" % quoted)
                            _drop_legacy_prunable_functions(cur)
                            baseline_proof = _prove_baseline(
                                cur,
                                migrations[0].sql,
                                allowed_extra_tables=_later_migration_tables(migrations),
                            )
                            mode = "authorized-existing-baseline-with-legacy-prune"
                        else:
                            mode = "authorized-existing-baseline"
                    else:
                        baseline_proof = None
                        mode = "fresh-bootstrap"
                    cur.execute(AUTHORITY_DDL)
                    current = 0
                else:
                    current = _inspect_versioned(cur, migrations, allow_behind=True)
                    baseline_proof = None
                    mode = "upgrade"

                applied: list[str] = []
                for ordinal, migration in enumerate(migrations[current:], start=current + 1):
                    if ordinal == 1 and baseline_proof is not None:
                        proof = baseline_proof
                    else:
                        cur.execute(migration.sql)
                        if ordinal == 1:
                            proof = _prove_baseline(cur, migration.sql)
                        elif migration.postcondition_sql:
                            cur.execute(migration.postcondition_sql)
                            row = cur.fetchone()
                            if row is None or row[0] is not True:
                                raise StoreError(
                                    "postcondition failed for PostgreSQL migration %s"
                                    % migration.migration_id
                                )
                            proof = {"postcondition": migration.postcondition_sql}
                        else:
                            raise StoreError(
                                "migration %s has no executable postcondition"
                                % migration.migration_id
                            )
                    cur.execute(
                        """
                        INSERT INTO schema_migrations (
                            ordinal, migration_id, checksum_sha256, applied_by, postcondition
                        ) VALUES (%s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            ordinal,
                            migration.migration_id,
                            migration.checksum_sha256,
                            actor,
                            __import__("json").dumps(proof, sort_keys=True),
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO schema_version (
                            singleton, ordinal, migration_id, checksum_sha256
                        ) VALUES (TRUE, %s, %s, %s)
                        ON CONFLICT (singleton) DO UPDATE SET
                            ordinal=EXCLUDED.ordinal,
                            migration_id=EXCLUDED.migration_id,
                            checksum_sha256=EXCLUDED.checksum_sha256,
                            updated_at=CURRENT_TIMESTAMP
                        """,
                        (ordinal, migration.migration_id, migration.checksum_sha256),
                    )
                    applied.append(migration.migration_id)

                current = _inspect_versioned(cur, migrations, allow_behind=False)
                final_proof = _prove_baseline(
                    cur,
                    render_bootstrap_schema(migrations[:current]),
                )
                return {
                    "status": "migrated" if applied else "verified",
                    "mode": mode,
                    "applied": applied,
                    "current_version": migrations[current - 1].migration_id,
                    "ordinal": current,
                    "proof": final_proof,
                    "legacy_schema_prune": legacy_prune,
                }
    except StoreError:
        raise
    except Exception as exc:
        raise StoreError(
            "PostgreSQL schema migration failed and was rolled back: %s" % exc
        ) from exc


def main(argv: Iterable[str] | None = None) -> int:
    """Deploy-oriented CLI; it never starts the control plane."""

    parser = argparse.ArgumentParser(description="Apply MAC PostgreSQL schema migrations")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("MAC_DATABASE_URL") or os.environ.get("MAC_DB"),
        help="PostgreSQL DSN (default: MAC_DATABASE_URL or MAC_DB)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="read-only preflight; report pending migrations without backup or DDL",
    )
    parser.add_argument("--applied-by", help="operator/deploy identity for migration application")
    parser.add_argument(
        "--authorize-existing-baseline",
        action="store_true",
        help="explicitly baseline a proved, known existing unversioned schema",
    )
    parser.add_argument(
        "--authorize-legacy-schema-prune",
        action="store_true",
        help="separately authorize dropping only reviewed legacy pre-baseline tables",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.database_url:
        parser.error("--database-url or MAC_DATABASE_URL/MAC_DB is required")
    if not args.status and not args.applied_by:
        parser.error("--applied-by is required unless --status is used")
    from mac.store_postgres import PostgresStore

    store = PostgresStore(args.database_url)
    try:
        if args.status:
            result = store.migration_status()
        else:
            result = store.apply_migrations(
                applied_by=args.applied_by,
                authorize_existing_baseline=args.authorize_existing_baseline,
                authorize_legacy_schema_prune=args.authorize_legacy_schema_prune,
            )
        print(__import__("json").dumps(result, sort_keys=True))
    except StoreError as exc:
        parser.exit(1, "mac-schema-migrate: %s\n" % exc)
    finally:
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
