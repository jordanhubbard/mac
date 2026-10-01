"""Focused live-PostgreSQL contracts for ADR 0021/0027 migration authority."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from mac.schema_migrations import MIGRATIONS, Migration, verify_schema
from mac.store import StoreError


pytestmark = pytest.mark.postgres


@contextmanager
def _fresh_store(pg_dsn: str):
    from mac.store_postgres import PostgresStore
    from mac.test_support import create_schema

    _schema, scoped_dsn = create_schema(pg_dsn)
    store = PostgresStore(scoped_dsn, pool_size=2, min_size=1)
    try:
        yield store
    finally:
        store.close()


def _install_unversioned_legacy_tables(store, *, include_unknown: bool = False) -> None:
    from mac.schema_migrations import LEGACY_PRUNABLE_TABLES

    with store._pool.connection() as conn:
        conn.execute(MIGRATIONS[0].sql)
        for table_name in sorted(LEGACY_PRUNABLE_TABLES):
            conn.execute('CREATE TABLE "%s" (id INTEGER PRIMARY KEY, payload TEXT)' % table_name)
            conn.execute(
                'INSERT INTO "%s" (id, payload) VALUES (1, %s)' % (table_name, "%s"),
                ("retained-by-backup",),
            )
        conn.execute(
            "ALTER TABLE evidence_attempt_verifications "
            "ADD COLUMN link_id INTEGER REFERENCES evidence_attempt_links(id)"
        )
        conn.execute(
            "INSERT INTO execution_cohort_assignments (id, payload) VALUES (2, %s)",
            ("second-live-row",),
        )
        if include_unknown:
            conn.execute("CREATE TABLE unknown_legacy_extra (id INTEGER PRIMARY KEY)")
        _install_leftover_work_package_task_triggers(conn)


def _install_leftover_work_package_task_triggers(conn) -> None:
    """Reproduce the live-hub leftover: a tasks trigger that queries a dropped table.

    DROP TABLE work_package_assignment_audit CASCADE does not remove this.
    """

    conn.execute(
        """
        CREATE OR REPLACE FUNCTION trg_work_package_task_claim_authority()
        RETURNS trigger AS $$
        BEGIN
            PERFORM 1 FROM work_package_assignment_audit LIMIT 1;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    conn.execute(
        """
        CREATE OR REPLACE FUNCTION trg_work_package_expiry_task_detach_guard()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.lease_id IS NOT NULL AND NEW.lease_id IS NULL THEN
                PERFORM 1 FROM work_package_assignment_audit LIMIT 1;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_work_package_task_claim_authority ON tasks")
    conn.execute(
        """
        CREATE TRIGGER trg_work_package_task_claim_authority
        BEFORE UPDATE ON tasks
        FOR EACH ROW EXECUTE FUNCTION trg_work_package_task_claim_authority()
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_work_package_expiry_task_detach_guard ON tasks")
    conn.execute(
        """
        CREATE TRIGGER trg_work_package_expiry_task_detach_guard
        BEFORE UPDATE ON tasks
        FOR EACH ROW EXECUTE FUNCTION trg_work_package_expiry_task_detach_guard()
        """
    )


def _function_exists(store, name: str) -> bool:
    row = store.query_one(
        "SELECT 1 AS ok FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = current_schema() AND p.proname = ?",
        (name,),
    )
    return row is not None


def _relation_exists(store, relation: str) -> bool:
    return store.query_one("SELECT to_regclass(?) AS relation", (relation,))["relation"] is not None


def test_fresh_database_bootstrap_records_version_and_append_only_ledger(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        preflight = store.migration_status()
        assert preflight["database_state"] == "fresh"
        assert preflight["requires_backup"] is True
        result = store.apply_migrations(applied_by="pytest:fresh")

        assert result["mode"] == "fresh-bootstrap"
        assert result["applied"] == [migration.migration_id for migration in MIGRATIONS]
        assert store.migration_status()["status"] == "current"
        assert store.migration_status()["requires_backup"] is False
        assert store.verify_schema()["current_version"] == MIGRATIONS[-1].migration_id
        row = store.query_one("SELECT * FROM schema_migrations WHERE ordinal = 1")
        assert row["checksum_sha256"] == MIGRATIONS[0].checksum_sha256
        assert row["applied_by"] == "pytest:fresh"
        with pytest.raises(StoreError, match="immutable"):
            store.execute("UPDATE schema_migrations SET applied_by = ?", ("tamper",))
        with pytest.raises(StoreError, match="append-only"):
            store.execute("DELETE FROM schema_migrations")


def test_known_existing_schema_requires_and_accepts_explicit_baseline(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        with store._pool.connection() as conn:
            conn.execute(MIGRATIONS[0].sql)

        with pytest.raises(StoreError, match="explicit.*authorize-existing-baseline"):
            store.initialize()
        with pytest.raises(StoreError, match="explicit.*authorize-existing-baseline"):
            store.apply_migrations(applied_by="pytest:no-authorization")
        result = store.apply_migrations(
            applied_by="pytest:authorized",
            authorize_existing_baseline=True,
        )
        assert result["mode"] == "authorized-existing-baseline"
        assert result["applied"] == [migration.migration_id for migration in MIGRATIONS]
        assert (
            store.query_one("SELECT to_regclass('dream_candidate_entries') AS relation")["relation"]
            is None
        )


def test_explicit_upgrade_applies_in_order_and_proves_postcondition(pg_dsn: str) -> None:
    upgrade = Migration(
        "0005_upgrade_probe",
        "CREATE TABLE migration_upgrade_probe (id INTEGER PRIMARY KEY)",
        "SELECT to_regclass('migration_upgrade_probe') IS NOT NULL",
    )
    chain = (*MIGRATIONS, upgrade)
    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:bootstrap")
        result = store.apply_migrations(applied_by="pytest:upgrade", migrations=chain)

        assert result["applied"] == ["0005_upgrade_probe"]
        assert result["current_version"] == "0005_upgrade_probe"
        assert (
            store.query_one("SELECT migration_id FROM schema_migrations WHERE ordinal = 5")[
                "migration_id"
            ]
            == "0005_upgrade_probe"
        )


def test_checksum_mismatch_refuses_startup(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:bootstrap")
        store.execute("DROP TRIGGER trg_schema_migrations_append_only ON schema_migrations")
        store.execute(
            "UPDATE schema_migrations SET checksum_sha256 = ? WHERE ordinal = 1",
            ("0" * 64,),
        )

        with pytest.raises(StoreError, match="checksum drift"):
            store.verify_schema()


def test_database_newer_than_binary_is_refused(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:bootstrap")
        store.execute(
            """
            INSERT INTO schema_migrations (
                ordinal, migration_id, checksum_sha256, applied_by, postcondition
            ) VALUES (?, ?, ?, ?, ?::jsonb)
            """,
            (len(MIGRATIONS) + 1, "9999_future", "f" * 64, "future-binary", "{}"),
        )
        store.execute(
            """
            UPDATE schema_version SET ordinal=?, migration_id=?, checksum_sha256=?,
                updated_at=CURRENT_TIMESTAMP WHERE singleton
            """,
            (len(MIGRATIONS) + 1, "9999_future", "f" * 64),
        )

        with pytest.raises(StoreError, match="newer than this binary"):
            store.verify_schema()


def test_schema_version_rejects_inconsistency_and_verification_detects_tampering(
    pg_dsn: str,
) -> None:
    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:bootstrap")
        with pytest.raises(StoreError, match="must match the latest migration"):
            store.execute(
                "UPDATE schema_version SET migration_id = ? WHERE singleton",
                ("0000_tampered",),
            )

        store.execute("DROP TRIGGER trg_schema_version_consistent ON schema_version")
        store.execute(
            "UPDATE schema_version SET migration_id = ? WHERE singleton",
            ("0000_tampered",),
        )
        with pytest.raises(StoreError, match="does not match the migration ledger"):
            store.verify_schema()


def test_missing_or_out_of_order_ledger_is_refused(pg_dsn: str) -> None:
    upgrade = Migration(
        "0005_order_probe",
        "CREATE TABLE migration_order_probe (id INTEGER PRIMARY KEY)",
        "SELECT to_regclass('migration_order_probe') IS NOT NULL",
    )
    chain = (*MIGRATIONS, upgrade)
    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:ordered", migrations=chain)
        store.execute("DROP TRIGGER trg_schema_migrations_append_only ON schema_migrations")
        store.execute("DELETE FROM schema_migrations WHERE ordinal = 1")

        with pytest.raises(StoreError, match="missing or out of order"):
            with store._pool.connection() as conn:
                verify_schema(conn, migrations=chain)


def test_failed_migration_rolls_back_ddl_and_receipt(pg_dsn: str) -> None:
    broken = Migration(
        "0005_rollback_probe",
        "CREATE TABLE migration_rollback_probe (id INTEGER PRIMARY KEY)",
        "SELECT FALSE",
    )
    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:bootstrap")

        with pytest.raises(StoreError, match="postcondition failed"):
            store.apply_migrations(applied_by="pytest:broken", migrations=(*MIGRATIONS, broken))

        assert (
            store.query_one("SELECT to_regclass('migration_rollback_probe') AS relation")[
                "relation"
            ]
            is None
        )
        assert store.query_one("SELECT COUNT(*) AS count FROM schema_migrations")["count"] == len(
            MIGRATIONS
        )
        assert store.query_one("SELECT ordinal FROM schema_version")["ordinal"] == len(MIGRATIONS)


def test_partial_unversioned_schema_is_never_silently_baselined(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        store.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")

        with pytest.raises(StoreError, match="partial or unknown"):
            store.migration_status()
        with pytest.raises(StoreError, match="partial or unknown"):
            store.apply_migrations(
                applied_by="pytest:partial",
                authorize_existing_baseline=True,
            )
        assert (
            store.query_one("SELECT to_regclass('schema_migrations') AS relation")["relation"]
            is None
        )


def test_read_only_startup_refuses_fresh_and_behind_databases(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        with pytest.raises(StoreError, match="fresh/uninitialized"):
            store.verify_schema()

        store.apply_migrations(applied_by="pytest:old-binary", migrations=MIGRATIONS[:1])
        status = store.migration_status()
        assert status["status"] == "pending"
        assert status["pending"] == [migration.migration_id for migration in MIGRATIONS[1:]]
        with pytest.raises(StoreError, match="behind this binary"):
            store.verify_schema()


def test_partial_or_empty_authority_is_refused(pg_dsn: str) -> None:
    from mac.schema_migrations import AUTHORITY_DDL

    with _fresh_store(pg_dsn) as store:
        store.execute("CREATE TABLE schema_version (singleton BOOLEAN)")
        with pytest.raises(StoreError, match="partial or corrupt"):
            store.migration_status()

    with _fresh_store(pg_dsn) as store:
        with store._pool.connection() as conn:
            conn.execute(AUTHORITY_DDL)
        with pytest.raises(StoreError, match="migration ledger is empty"):
            store.migration_status()


def test_pending_migration_without_postcondition_is_refused(pg_dsn: str) -> None:
    missing_proof = Migration(
        "0005_missing_proof",
        "CREATE TABLE migration_missing_proof (id INTEGER PRIMARY KEY)",
    )
    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:bootstrap")
        with pytest.raises(StoreError, match="has no executable postcondition"):
            store.apply_migrations(
                applied_by="pytest:missing-proof",
                migrations=(*MIGRATIONS, missing_proof),
            )
        assert (
            store.query_one("SELECT to_regclass('migration_missing_proof') AS relation")["relation"]
            is None
        )


def test_preflight_reports_exact_reviewed_legacy_prune_counts_read_only(pg_dsn: str) -> None:
    from mac.schema_migrations import LEGACY_PRUNABLE_TABLES

    with _fresh_store(pg_dsn) as store:
        _install_unversioned_legacy_tables(store)

        status = store.migration_status()

        assert status["database_state"] == "existing-unversioned"
        assert status["requires_backup"] is True
        assert status["requires_existing_baseline_authority"] is True
        assert status["requires_legacy_schema_prune_authority"] is True
        prune = status["legacy_schema_prune"]
        assert prune["required"] is True
        assert prune["requires_backup"] is True
        assert prune["requires_authorization"] is True
        assert prune["table_names"] == sorted(LEGACY_PRUNABLE_TABLES)
        assert prune["total_rows"] == len(LEGACY_PRUNABLE_TABLES) + 1
        assert {item["table"]: item["row_count"] for item in prune["tables"]}[
            "execution_cohort_assignments"
        ] == 2
        assert all(_relation_exists(store, table) for table in LEGACY_PRUNABLE_TABLES)
        assert not _relation_exists(store, "schema_migrations")


def test_legacy_prune_requires_separate_authority_and_accepts_rows(pg_dsn: str) -> None:
    from mac.schema_migrations import LEGACY_PRUNABLE_TABLES

    with _fresh_store(pg_dsn) as store:
        _install_unversioned_legacy_tables(store)

        with pytest.raises(StoreError, match="authorize-legacy-schema-prune"):
            store.apply_migrations(
                applied_by="pytest:baseline-only",
                authorize_existing_baseline=True,
            )
        assert all(_relation_exists(store, table) for table in LEGACY_PRUNABLE_TABLES)
        assert not _relation_exists(store, "schema_migrations")

        result = store.apply_migrations(
            applied_by="pytest:authorized-prune",
            authorize_existing_baseline=True,
            authorize_legacy_schema_prune=True,
        )

        assert result["mode"] == "authorized-existing-baseline-with-legacy-prune"
        assert result["legacy_schema_prune"]["table_names"] == sorted(LEGACY_PRUNABLE_TABLES)
        assert result["legacy_schema_prune"]["total_rows"] == len(LEGACY_PRUNABLE_TABLES) + 1
        assert not any(_relation_exists(store, table) for table in LEGACY_PRUNABLE_TABLES)
        assert not _function_exists(store, "trg_work_package_task_claim_authority")
        assert not _function_exists(store, "trg_work_package_expiry_task_detach_guard")
        assert store.verify_schema()["status"] == "verified"


def test_unknown_extra_blocks_before_legacy_prune_mutation(pg_dsn: str) -> None:
    from mac.schema_migrations import LEGACY_PRUNABLE_TABLES

    with _fresh_store(pg_dsn) as store:
        _install_unversioned_legacy_tables(store, include_unknown=True)

        with pytest.raises(StoreError, match="unknown_legacy_extra"):
            store.migration_status()
        with pytest.raises(StoreError, match="unknown_legacy_extra"):
            store.apply_migrations(
                applied_by="pytest:unknown-extra",
                authorize_existing_baseline=True,
                authorize_legacy_schema_prune=True,
            )

        assert all(_relation_exists(store, table) for table in LEGACY_PRUNABLE_TABLES)
        assert _relation_exists(store, "unknown_legacy_extra")
        assert not _relation_exists(store, "schema_migrations")


def test_failure_after_legacy_drop_rolls_back_tables_rows_and_ledger(pg_dsn: str) -> None:
    from mac.schema_migrations import LEGACY_PRUNABLE_TABLES

    broken = Migration(
        "0005_legacy_prune_rollback_probe",
        "CREATE TABLE legacy_prune_rollback_probe (id INTEGER PRIMARY KEY)",
        "SELECT FALSE",
    )
    with _fresh_store(pg_dsn) as store:
        _install_unversioned_legacy_tables(store)

        with pytest.raises(StoreError, match="postcondition failed"):
            store.apply_migrations(
                applied_by="pytest:rollback",
                authorize_existing_baseline=True,
                authorize_legacy_schema_prune=True,
                migrations=(*MIGRATIONS, broken),
            )

        assert all(_relation_exists(store, table) for table in LEGACY_PRUNABLE_TABLES)
        assert (
            store.query_one("SELECT COUNT(*) AS count FROM execution_cohort_assignments")["count"]
            == 2
        )
        assert not _relation_exists(store, "schema_migrations")
        assert not _relation_exists(store, "legacy_prune_rollback_probe")


def test_upgrade_drops_leftover_work_package_task_triggers(pg_dsn: str) -> None:
    """A hub already on 0002 still has the tasks triggers after table prune."""

    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(
            applied_by="pytest:through-0002",
            migrations=MIGRATIONS[:2],
        )
        with store._pool.connection() as conn:
            _install_leftover_work_package_task_triggers(conn)
        assert _function_exists(store, "trg_work_package_task_claim_authority")
        assert _function_exists(store, "trg_work_package_expiry_task_detach_guard")

        result = store.apply_migrations(applied_by="pytest:drop-leftovers")

        assert result["applied"] == [
            "0003_drop_leftover_work_package_triggers",
            "0004_drop_removed_feature_tables",
        ]
        assert not _function_exists(store, "trg_work_package_task_claim_authority")
        assert not _function_exists(store, "trg_work_package_expiry_task_detach_guard")
        assert store.verify_schema()["status"] == "verified"


_REMOVED_FEATURE_TABLES = (
    "scientific_decisions",
    "scientific_observations",
    "scientific_assignments",
    "scientific_experiments",
    "scientific_policies",
    "scientific_optimizer_events",
    "scientific_optimizer_locks",
    "dream_candidate_entries",
    "dream_runs",
    "nap_runs",
    "nap_schedules",
)


def _populate_removed_feature_tables(conn) -> None:
    """At least one row in every table 0004 drops, with the keys between them."""

    now = "2026-09-30T00:00:00+00:00"
    conn.execute(
        "INSERT INTO machines (id, hostname, labels, resources, trusted, created_at, "
        "updated_at, last_seen_at) VALUES ('m1', 'host', '{}', '{}', 1, %s, %s, %s)",
        (now, now, now),
    )
    conn.execute(
        "INSERT INTO agents (id, machine_id, name, capabilities, resources, status, "
        "health_status, created_at, updated_at, last_seen_at) "
        "VALUES ('a1', 'm1', 'agent', '[]', '{}', 'idle', 'healthy', %s, %s, %s)",
        (now, now, now),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, description, state, required_capabilities, "
        "dependencies, metadata, created_at, updated_at) "
        "VALUES ('t1', 'task', '', 'open', '[]', '[]', '{}', %s, %s)",
        (now, now),
    )
    for policy_id, name in (("p1", "control"), ("p2", "treatment")):
        conn.execute(
            "INSERT INTO scientific_policies (id, schema_version, project, name, version, "
            "status, created_by, created_at, updated_at) "
            "VALUES (%s, 'v1', 'mac', %s, 1, 'draft', 'test', %s, %s)",
            (policy_id, name, now, now),
        )
    conn.execute(
        "INSERT INTO scientific_experiments (id, schema_version, project, name, hypothesis, "
        "state, control_policy_id, treatment_policy_id, primary_metric, direction, "
        "min_samples_per_arm, max_samples_per_arm, exploration_fraction, "
        "outcome_horizon_seconds, created_by, created_at, updated_at) "
        "VALUES ('e1', 'v1', 'mac', 'exp', 'h', 'running', 'p1', 'p2', 'm', 'up', "
        "1, 2, 0.1, 60, 'test', %s, %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO scientific_assignments (experiment_id, task_id, arm, policy_id, phase, "
        "propensity, assignment, assigned_at) "
        "VALUES ('e1', 't1', 'control', 'p1', 'explore', 0.5, '{}', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO scientific_observations (experiment_id, task_id, arm, phase, metrics, "
        "observed_at) VALUES ('e1', 't1', 'control', 'explore', '{}', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO scientific_decisions (id, experiment_id, status, decision, actor, "
        "created_at) VALUES ('d1', 'e1', 'final', '{}', 'test', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO scientific_optimizer_events (id, subject_type, subject_id, event_type, "
        "actor, created_at) VALUES ('ev1', 'experiment', 'e1', 'created', 'test', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO scientific_optimizer_locks (name, owner_id, lease_expires_at, "
        "updated_at) VALUES ('tick', 'hub', %s, %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO dream_runs (id, status, state, policy, gates, stats, reflections, "
        "errors, created_at) VALUES ('r1', 'done', 'done', '{}', '{}', '{}', '[]', '[]', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO dream_candidate_entries (id, run_id, kind, statement, created_at) "
        "VALUES ('c1', 'r1', 'lesson', 'statement', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO nap_schedules (agent_id, offset_minutes, updated_at) VALUES ('a1', 5, %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO nap_runs (id, agent_id, status, started_at, detail, created_at, "
        "updated_at) VALUES ('n1', 'a1', 'done', %s, '{}', %s, %s)",
        (now, now, now),
    )


def test_upgrade_from_0003_drops_removed_feature_tables_with_rows(pg_dsn: str) -> None:
    """A hub at 0003 still holds rows in the removed features' tables."""

    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:through-0003", migrations=MIGRATIONS[:3])
        with store._pool.connection() as conn:
            _populate_removed_feature_tables(conn)
        for table in _REMOVED_FEATURE_TABLES:
            assert store.query_one('SELECT COUNT(*) AS n FROM "%s"' % table)["n"] >= 1, table

        status = store.migration_status()
        assert status["pending"] == ["0004_drop_removed_feature_tables"]
        assert status["requires_backup"] is True

        result = store.apply_migrations(applied_by="pytest:drop-removed-features")

        assert result["applied"] == ["0004_drop_removed_feature_tables"]
        assert not [table for table in _REMOVED_FEATURE_TABLES if _relation_exists(store, table)]
        # Rows outside the dropped tables survive, and 0002's re-proved
        # postcondition still holds now that its tables are gone.
        assert store.query_one("SELECT COUNT(*) AS n FROM agents")["n"] == 1
        assert store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 1
        verified = store.verify_schema()
        assert verified["current_version"] == "0004_drop_removed_feature_tables"
        assert "0002_dream_candidate_store" in verified["proof"]["postconditions"]


def test_fresh_bootstrap_leaves_no_removed_feature_table(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        store.initialize()
        assert not [table for table in _REMOVED_FEATURE_TABLES if _relation_exists(store, table)]
        assert store.verify_schema()["status"] == "verified"
