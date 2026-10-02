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


def _probe_id(name: str) -> str:
    """A probe migration id at the next free ordinal after the binary's chain."""

    return "%04d_%s" % (len(MIGRATIONS) + 1, name)


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
        _probe_id("upgrade_probe"),
        "CREATE TABLE migration_upgrade_probe (id INTEGER PRIMARY KEY)",
        "SELECT to_regclass('migration_upgrade_probe') IS NOT NULL",
    )
    chain = (*MIGRATIONS, upgrade)
    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:bootstrap")
        result = store.apply_migrations(applied_by="pytest:upgrade", migrations=chain)

        assert result["applied"] == [_probe_id("upgrade_probe")]
        assert result["current_version"] == _probe_id("upgrade_probe")
        assert store.query_one(
            "SELECT migration_id FROM schema_migrations WHERE ordinal = ?",
            (len(MIGRATIONS) + 1,),
        )["migration_id"] == _probe_id("upgrade_probe")


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
        _probe_id("order_probe"),
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
        _probe_id("rollback_probe"),
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
        _probe_id("missing_proof"),
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
        _probe_id("legacy_prune_rollback_probe"),
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
            "0005_drop_native_merge_queue_tables",
            "0006_drop_rollout_and_deploy_tables",
            "0007_drop_agent_provisioning_requests",
            "0008_drop_self_upgrade_and_release_epoch_tables",
            "0009_slim_worker_credentials",
            "0010_inference_tokens",
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
        assert status["pending"] == [
            "0004_drop_removed_feature_tables",
            "0005_drop_native_merge_queue_tables",
            "0006_drop_rollout_and_deploy_tables",
            "0007_drop_agent_provisioning_requests",
            "0008_drop_self_upgrade_and_release_epoch_tables",
            "0009_slim_worker_credentials",
            "0010_inference_tokens",
        ]
        assert status["requires_backup"] is True

        result = store.apply_migrations(applied_by="pytest:drop-removed-features")

        assert result["applied"] == [
            "0004_drop_removed_feature_tables",
            "0005_drop_native_merge_queue_tables",
            "0006_drop_rollout_and_deploy_tables",
            "0007_drop_agent_provisioning_requests",
            "0008_drop_self_upgrade_and_release_epoch_tables",
            "0009_slim_worker_credentials",
            "0010_inference_tokens",
        ]
        assert not [table for table in _REMOVED_FEATURE_TABLES if _relation_exists(store, table)]
        # Rows outside the dropped tables survive, and 0002's re-proved
        # postcondition still holds now that its tables are gone.
        assert store.query_one("SELECT COUNT(*) AS n FROM agents")["n"] == 1
        assert store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 1
        verified = store.verify_schema()
        assert verified["current_version"] == "0010_inference_tokens"
        assert "0002_dream_candidate_store" in verified["proof"]["postconditions"]


def test_fresh_bootstrap_leaves_no_removed_feature_table(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        store.initialize()
        assert not [table for table in _REMOVED_FEATURE_TABLES if _relation_exists(store, table)]
        assert store.verify_schema()["status"] == "verified"


_NATIVE_MERGE_QUEUE_TABLES = ("merge_queue_entries", "merge_queue_windows")


def _populate_native_merge_queue_tables(conn) -> None:
    """A queued entry and its window, as a hub at 0004 could still hold them."""

    now = "2026-10-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO tasks (id, title, description, state, required_capabilities, "
        "dependencies, metadata, created_at, updated_at) "
        "VALUES ('t1', 'task', '', 'reviewing', '[]', '[]', '{}', %s, %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO merge_queue_entries (id, repository, branch, task_id, head_sha, "
        "state, position, created_at, updated_at) "
        "VALUES ('mqe1', 'jordanhubbard/mac', 'main', 't1', %s, 'queued', 0, %s, %s)",
        ("a" * 40, now, now),
    )
    conn.execute(
        "INSERT INTO merge_queue_windows (repository, branch, window_size, updated_at) "
        "VALUES ('jordanhubbard/mac', 'main', 2, %s)",
        (now,),
    )


def test_upgrade_from_0004_drops_native_merge_queue_tables_with_rows(pg_dsn: str) -> None:
    """A hub at 0004 still holds merge-queue rows; 0005 drops both tables."""

    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:through-0004", migrations=MIGRATIONS[:4])
        with store._pool.connection() as conn:
            _populate_native_merge_queue_tables(conn)
        for table in _NATIVE_MERGE_QUEUE_TABLES:
            assert store.query_one('SELECT COUNT(*) AS n FROM "%s"' % table)["n"] == 1, table

        status = store.migration_status()
        assert status["pending"] == [
            "0005_drop_native_merge_queue_tables",
            "0006_drop_rollout_and_deploy_tables",
            "0007_drop_agent_provisioning_requests",
            "0008_drop_self_upgrade_and_release_epoch_tables",
            "0009_slim_worker_credentials",
            "0010_inference_tokens",
        ]
        assert status["requires_backup"] is True

        result = store.apply_migrations(applied_by="pytest:drop-native-merge-queue")

        assert result["applied"] == [
            "0005_drop_native_merge_queue_tables",
            "0006_drop_rollout_and_deploy_tables",
            "0007_drop_agent_provisioning_requests",
            "0008_drop_self_upgrade_and_release_epoch_tables",
            "0009_slim_worker_credentials",
            "0010_inference_tokens",
        ]
        assert not [t for t in _NATIVE_MERGE_QUEUE_TABLES if _relation_exists(store, t)]
        # The task the queue entry pointed at is untouched: it is still
        # REVIEWING and the land loop picks it up on the next tick.
        assert store.query_one("SELECT state FROM tasks WHERE id = 't1'")["state"] == "reviewing"
        verified = store.verify_schema()
        assert verified["status"] == "verified"
        assert verified["current_version"] == "0010_inference_tokens"


def test_fresh_bootstrap_leaves_no_native_merge_queue_table(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        store.initialize()
        assert not [t for t in _NATIVE_MERGE_QUEUE_TABLES if _relation_exists(store, t)]
        verified = store.verify_schema()
        assert verified["status"] == "verified"
        assert verified["current_version"] == "0010_inference_tokens"


_ROLLOUT_AND_DEPLOY_TABLES = (
    "rollout_events",
    "rollouts",
    "deployments",
    "environment_events",
    "managed_task_publication_rollout",
)


def _populate_rollout_and_deploy_tables(conn) -> None:
    """A row in every table 0006 drops, plus the kept tables they point at."""

    now = "2026-10-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO rollouts (id, version, strategy, status, target_percent, "
        "created_by, created_at, updated_at) "
        "VALUES ('rollout1', 'v1', 'canary', 'planned', 10, 'test', %s, %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO rollout_events (id, rollout_id, event_type, actor, detail, created_at) "
        "VALUES ('rev1', 'rollout1', 'rollout.created', 'test', '{}', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO environments (id, name, metadata, created_by, created_at, updated_at) "
        "VALUES ('env1', 'staging', '{}', 'test', %s, %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO environment_events (id, environment_id, event_type, actor, detail, "
        "created_at) VALUES ('eev1', 'env1', 'environment.registered', 'test', '{}', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO artifacts (id, kind, digest, uri, signers, metadata, created_by, "
        "created_at, updated_at) "
        "VALUES ('art1', 'wheel', %s, 'file:///tmp/a.whl', '[]', '{}', 'test', %s, %s)",
        ("sha256:" + "b" * 64, now, now),
    )
    conn.execute(
        "INSERT INTO deployments (id, environment_id, artifact_id, status, deployed_by, "
        "deployed_at) VALUES ('dep1', 'env1', 'art1', 'active', 'test', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO managed_task_publication_rollout (singleton_key, revision, crossed_by, "
        "crossed_at) VALUES ('fleet', 1, 'test', %s)",
        (now,),
    )


def test_upgrade_from_0005_drops_rollout_and_deploy_tables_with_rows(pg_dsn: str) -> None:
    """A hub at 0005 still has the rollout/deploy tables; 0006 drops them."""

    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:through-0005", migrations=MIGRATIONS[:5])
        with store._pool.connection() as conn:
            _populate_rollout_and_deploy_tables(conn)
        for table in _ROLLOUT_AND_DEPLOY_TABLES:
            assert store.query_one('SELECT COUNT(*) AS n FROM "%s"' % table)["n"] == 1, table
        subjects = store.query_all("SELECT DISTINCT subject_type FROM events")
        assert {"rollout", "environment"} <= {row["subject_type"] for row in subjects}

        status = store.migration_status()
        assert status["pending"] == [
            "0006_drop_rollout_and_deploy_tables",
            "0007_drop_agent_provisioning_requests",
            "0008_drop_self_upgrade_and_release_epoch_tables",
            "0009_slim_worker_credentials",
            "0010_inference_tokens",
        ]
        assert status["requires_backup"] is True

        result = store.apply_migrations(
            applied_by="pytest:drop-rollout-and-deploy", migrations=MIGRATIONS[:6]
        )

        assert result["applied"] == ["0006_drop_rollout_and_deploy_tables"]
        assert not [t for t in _ROLLOUT_AND_DEPLOY_TABLES if _relation_exists(store, t)]
        # The tables other code still used at 0006 keep their rows.
        assert store.query_one("SELECT COUNT(*) AS n FROM environments")["n"] == 1
        assert store.query_one("SELECT COUNT(*) AS n FROM artifacts")["n"] == 1
        # The unified events view survives without the dropped sources.
        assert _relation_exists(store, "events")
        subjects = store.query_all("SELECT DISTINCT subject_type FROM events")
        assert not {"rollout", "environment"} & {row["subject_type"] for row in subjects}

        result = store.apply_migrations(applied_by="pytest:through-head")

        assert result["applied"] == [
            "0007_drop_agent_provisioning_requests",
            "0008_drop_self_upgrade_and_release_epoch_tables",
            "0009_slim_worker_credentials",
            "0010_inference_tokens",
        ]
        # 0008 drops environments once nothing references it.
        assert not _relation_exists(store, "environments")
        assert store.query_one("SELECT COUNT(*) AS n FROM artifacts")["n"] == 1
        verified = store.verify_schema()
        assert verified["status"] == "verified"
        assert verified["current_version"] == "0010_inference_tokens"


def test_fresh_bootstrap_leaves_no_rollout_or_deploy_table(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        store.initialize()
        assert not [t for t in _ROLLOUT_AND_DEPLOY_TABLES if _relation_exists(store, t)]
        assert _relation_exists(store, "events")
        assert _relation_exists(store, "artifacts")
        verified = store.verify_schema()
        assert verified["status"] == "verified"
        assert verified["current_version"] == "0010_inference_tokens"


def _populate_agent_provisioning_requests(conn) -> None:
    """Pending and fulfilled demand rows, as a hub at 0006 could still hold them."""

    now = "2026-10-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO tasks (id, title, description, state, required_capabilities, "
        "dependencies, metadata, created_at, updated_at) "
        "VALUES ('t1', 'task', '', 'open', '[\"gpu\"]', '[]', '{}', %s, %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO agent_provisioning_requests (id, status, reason, capabilities, "
        "task_id, created_at, updated_at) "
        "VALUES ('pr1', 'pending', 'dispatch.no_eligible_agent', '[\"gpu\"]', 't1', %s, %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO agent_provisioning_requests (id, status, reason, created_at, "
        "updated_at, closed_at) "
        "VALUES ('pr2', 'cancelled', 'service_role:media', %s, %s, %s)",
        (now, now, now),
    )


def test_upgrade_from_0006_drops_agent_provisioning_requests_with_rows(pg_dsn: str) -> None:
    """A hub at 0006 still holds provisioning demand rows; 0007 drops the table."""

    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:through-0006", migrations=MIGRATIONS[:6])
        with store._pool.connection() as conn:
            _populate_agent_provisioning_requests(conn)
        assert store.query_one("SELECT COUNT(*) AS n FROM agent_provisioning_requests")["n"] == 2

        status = store.migration_status()
        assert status["pending"] == [
            "0007_drop_agent_provisioning_requests",
            "0008_drop_self_upgrade_and_release_epoch_tables",
            "0009_slim_worker_credentials",
            "0010_inference_tokens",
        ]
        assert status["requires_backup"] is True

        result = store.apply_migrations(applied_by="pytest:drop-provisioning-requests")

        assert result["applied"] == [
            "0007_drop_agent_provisioning_requests",
            "0008_drop_self_upgrade_and_release_epoch_tables",
            "0009_slim_worker_credentials",
            "0010_inference_tokens",
        ]
        assert not _relation_exists(store, "agent_provisioning_requests")
        # The task a request pointed at is untouched and still dispatchable.
        assert store.query_one("SELECT state FROM tasks WHERE id = 't1'")["state"] == "open"
        verified = store.verify_schema()
        assert verified["status"] == "verified"
        assert verified["current_version"] == "0010_inference_tokens"


def test_fresh_bootstrap_leaves_no_agent_provisioning_requests_table(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        store.initialize()
        assert not _relation_exists(store, "agent_provisioning_requests")
        verified = store.verify_schema()
        assert verified["status"] == "verified"
        assert verified["current_version"] == "0010_inference_tokens"


# Children before parents: the order 0008 drops them in.
_SELF_UPGRADE_AND_RELEASE_EPOCH_TABLES = (
    "fleet_upgrade_events",
    "fleet_upgrades",
    "source_convergence_nodes",
    "source_convergence_controller_leases",
    "fleet_release_attestation_candidates",
    "fleet_release_epoch_agents",
    "fleet_release_epochs",
    "fleet_release_admission_episodes",
    "fleet_desired_source_idempotency",
    "fleet_desired_source_transitions",
    "fleet_desired_source_states",
    "source_releases",
    "environments",
)
_SELF_UPGRADE_TRIGGER_FUNCTIONS = (
    "_trg_source_releases_sha_immutable",
    "_trg_fleet_desired_source_gen_monotonic",
)


def _populate_self_upgrade_and_release_epoch_tables(conn) -> None:
    """A row in every table 0008 drops, wired through every foreign key."""

    now = "2026-10-01T00:00:00+00:00"
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
        "INSERT INTO fleets (id, name, description, created_at, updated_at) "
        "VALUES ('f1', 'fleet', '', %s, %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO worker_credentials (id, agent_id, credential_version, token_hash, "
        "token_fingerprint, scopes, environment, state, issued_at, expires_at, created_by, "
        "updated_at) VALUES ('p1', 'a1', 1, 'h', 'fp', '[]', 'vm', 'active', %s, %s, "
        "'test', %s)",
        (now, now, now),
    )
    conn.execute(
        "INSERT INTO environments (id, name, metadata, created_by, created_at, updated_at) "
        "VALUES ('env1', 'staging', '{}', 'test', %s, %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO source_releases (id, repository_id, repository_name, "
        "canonical_remote_url, commit_sha, canonical_ref, tree_digest, created_by, "
        "created_at, updated_at) VALUES ('rel1', 'repo1', 'mac', "
        "'https://example.invalid/mac.git', %s, 'refs/tags/v1', 'sha256:t', 'test', %s, %s)",
        ("a" * 40, now, now),
    )
    conn.execute(
        "INSERT INTO fleet_desired_source_states (id, fleet_id, environment_id, generation, "
        "release_id, actor, created_at, updated_at) "
        "VALUES ('ds1', 'f1', 'env1', 1, 'rel1', 'test', %s, %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO fleet_desired_source_transitions (id, desired_source_state_id, "
        "to_generation, release_id, actor, created_at) "
        "VALUES ('dst1', 'ds1', 1, 'rel1', 'test', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO fleet_desired_source_idempotency (id, scope_key, request_id, "
        "desired_source_state_id, generation, created_at) "
        "VALUES ('dsi1', 'fleet:f1', 'req1', 'ds1', 1, %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO fleet_release_epochs (epoch_id, request_sha256, identity_sha256, "
        "identity_payload, state, policy_snapshot, actor, prepared_at) "
        "VALUES ('ep1', 'r', 'i', '{}', 'aborted', '{}', 'test', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO fleet_release_epoch_agents (epoch_id, agent_id, ordinal, "
        "prior_dispatch_hold, epoch_hold_reason, epoch_hold_at, "
        "prior_active_service_claim_ids, generation, baseline_seen, principal_id, "
        "principal_version, principal_fingerprint, prior_live_principal_ids, "
        "prior_attestation_ciphertext_sha256, report_executor_action, "
        "prior_report_executor_projection_sha256, created_at) "
        "VALUES ('ep1', 'a1', 0, 0, 'mac:fleet-release:ep1', %s, '[]', 'g', 'b', 'p1', "
        "1, 'fp', '[]', 'sha', 'preserve', 'sha', %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO fleet_release_attestation_candidates (epoch_id, agent_id, "
        "key_ciphertext, key_fingerprint, created_at) VALUES ('ep1', 'a1', 'c', 'k', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO fleet_release_admission_episodes (id, barrier_resource_digest, "
        "owner_kind, waiter_kind, wait_started_at, outcome, created_at, updated_at) "
        "VALUES ('ae1', 'd', 'publisher', 'epoch_opener', %s, 'admitted', %s, %s)",
        (now, now, now),
    )
    conn.execute(
        "INSERT INTO fleet_upgrades (id, idempotency_key, request_sha256, fleet_id, "
        "requested_by_human, requested_by_principal, target_policy, reason, state, phase, "
        "requested_release_id, epoch_id, desired_source_state_id, created_at, updated_at) "
        "VALUES ('up1', 'idem', 'r', 'f1', 'human', 'principal', 'approved-current', "
        "'test', 'failed', 'stage', 'rel1', 'ep1', 'ds1', %s, %s)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO fleet_upgrade_events (id, upgrade_id, event_type, phase, actor, "
        "created_at) VALUES ('ue1', 'up1', 'fleet_upgrade.requested', 'stage', 'test', %s)",
        (now,),
    )
    conn.execute(
        "INSERT INTO source_convergence_nodes (id, desired_source_state_id, fleet_id, "
        "agent_id, desired_generation, release_id, desired_sha, action, plan_digest, phase, "
        "created_at, updated_at) VALUES ('scn1', 'ds1', 'f1', 'a1', 1, 'rel1', %s, "
        "'noop', 'd', 'converged', %s, %s)",
        ("a" * 40, now, now),
    )
    conn.execute(
        "INSERT INTO source_convergence_controller_leases (scope_key, owner_id, "
        "expires_at, updated_at) VALUES ('global', 'hub', %s, %s)",
        (now, now),
    )


def test_upgrade_from_0007_drops_self_upgrade_and_release_epoch_tables_with_rows(
    pg_dsn: str,
) -> None:
    """A hub at 0007 holds rows in every self-upgrade table; 0008 drops them all."""

    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:through-0007", migrations=MIGRATIONS[:7])
        with store._pool.connection() as conn:
            _populate_self_upgrade_and_release_epoch_tables(conn)
        for table in _SELF_UPGRADE_AND_RELEASE_EPOCH_TABLES:
            assert store.query_one('SELECT COUNT(*) AS n FROM "%s"' % table)["n"] == 1, table
        for function in _SELF_UPGRADE_TRIGGER_FUNCTIONS:
            assert _function_exists(store, function), function

        status = store.migration_status()
        assert status["pending"] == [
            "0008_drop_self_upgrade_and_release_epoch_tables",
            "0009_slim_worker_credentials",
            "0010_inference_tokens",
        ]
        assert status["requires_backup"] is True

        result = store.apply_migrations(
            applied_by="pytest:drop-self-upgrade", migrations=MIGRATIONS[:8]
        )

        assert result["applied"] == ["0008_drop_self_upgrade_and_release_epoch_tables"]
        assert not [t for t in _SELF_UPGRADE_AND_RELEASE_EPOCH_TABLES if _relation_exists(store, t)]
        assert not [f for f in _SELF_UPGRADE_TRIGGER_FUNCTIONS if _function_exists(store, f)]
        # The rows the dropped tables pointed at are untouched.
        assert store.query_one("SELECT COUNT(*) AS n FROM agents")["n"] == 1
        assert store.query_one("SELECT COUNT(*) AS n FROM fleets")["n"] == 1
        assert store.query_one("SELECT COUNT(*) AS n FROM machines")["n"] == 1
        assert store.query_one("SELECT COUNT(*) AS n FROM worker_credentials")["n"] == 1

        result = store.apply_migrations(applied_by="pytest:through-head")

        assert result["applied"] == ["0009_slim_worker_credentials", "0010_inference_tokens"]
        assert store.query_one("SELECT COUNT(*) AS n FROM worker_credentials")["n"] == 1
        verified = store.verify_schema()
        assert verified["status"] == "verified"
        assert verified["current_version"] == "0010_inference_tokens"


def test_fresh_bootstrap_leaves_no_self_upgrade_or_release_epoch_table(pg_dsn: str) -> None:
    with _fresh_store(pg_dsn) as store:
        store.initialize()
        assert not [t for t in _SELF_UPGRADE_AND_RELEASE_EPOCH_TABLES if _relation_exists(store, t)]
        assert not [f for f in _SELF_UPGRADE_TRIGGER_FUNCTIONS if _function_exists(store, f)]
        verified = store.verify_schema()
        assert verified["status"] == "verified"
        assert verified["current_version"] == "0010_inference_tokens"


_SLIMMED_WORKER_CREDENTIAL_TABLES = ("worker_credential_events", "worker_credential_policy_state")
_SLIMMED_WORKER_CREDENTIAL_COLUMNS = (
    "fleet",
    "environment",
    "expected_source_commit",
    "expected_runtime_digest",
    "required_capabilities",
    "package_capable",
    "destination",
)


def _worker_credential_columns(store) -> set:
    return {
        row["column_name"]
        for row in store.query_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'worker_credentials'"
        )
    }


def _populate_live_worker_credentials(conn, tokens) -> None:
    """The live hub's shape at 0008: one active, pinned token per worker, plus
    a superseded predecessor, its events and the compatibility policy row."""

    from mac.worker_credentials import _fingerprint, _token_hash

    now = "2026-10-01T00:00:00+00:00"
    expires = "2027-10-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO machines (id, hostname, labels, resources, trusted, created_at, "
        "updated_at, last_seen_at) VALUES ('m1', 'host', '{}', '{}', 1, %s, %s, %s)",
        (now, now, now),
    )
    for index, token in enumerate(tokens):
        agent = "agent_%d" % index
        conn.execute(
            "INSERT INTO agents (id, machine_id, name, capabilities, resources, status, "
            "health_status, created_at, updated_at, last_seen_at) "
            "VALUES (%s, 'm1', %s, '[]', '{}', 'idle', 'healthy', %s, %s, %s)",
            (agent, agent, now, now, now),
        )
        old_id, new_id = "worker-%d-v0001" % index, "worker-%d-v0002" % index
        for principal, version, state, token_value in (
            (old_id, 1, "superseded", "old-" + token),
            (new_id, 2, "active", token),
        ):
            conn.execute(
                "INSERT INTO worker_credentials (id, agent_id, fleet, credential_version, "
                "token_hash, token_fingerprint, scopes, environment, expected_source_commit, "
                "expected_runtime_digest, required_capabilities, package_capable, state, "
                "destination, issued_at, expires_at, activated_at, created_by, updated_at) "
                "VALUES (%s, %s, 'mac', %s, %s, %s, %s, 'vm', %s, %s, '[\"python\"]', TRUE, "
                "%s, 'vm_env', %s, %s, %s, 'fleet-deploy', %s)",
                (
                    principal,
                    agent,
                    version,
                    _token_hash(token_value),
                    _fingerprint(token_value),
                    '["agent", "dispatch", "read", "write", "review:advance"]',
                    "a" * 40,
                    "d" * 64,
                    state,
                    now,
                    expires,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO worker_credential_events (id, principal_id, agent_id, "
                "event_type, actor, detail, created_at) "
                "VALUES (%s, %s, %s, 'worker_credential.activated', 'test', '{}', %s)",
                ("ev-" + principal, principal, agent, now),
            )
        conn.execute(
            "UPDATE worker_credentials SET superseded_by = %s, revoked_at = %s WHERE id = %s",
            (new_id, now, old_id),
        )
    conn.execute(
        "INSERT INTO worker_credential_policy_state (singleton_key, mode, ready_agent_ids, "
        "revision, updated_by, updated_at) VALUES ('fleet', 'compatibility', '[]', 1, "
        "'test', %s)",
        (now,),
    )


def test_upgrade_from_0008_keeps_every_active_worker_token_authenticating(
    pg_dsn: str,
) -> None:
    """The live hub holds 7 active worker tokens; after 0009 each still resolves."""

    from mac.worker_credentials import WorkerCredentialPrincipalProvider, _token_hash

    tokens = ["mac_worker_live_%d" % index for index in range(7)]
    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:through-0008", migrations=MIGRATIONS[:8])
        with store._pool.connection() as conn:
            _populate_live_worker_credentials(conn, tokens)
        assert set(_SLIMMED_WORKER_CREDENTIAL_COLUMNS) <= _worker_credential_columns(store)

        status = store.migration_status()
        assert status["pending"] == ["0009_slim_worker_credentials", "0010_inference_tokens"]
        assert status["requires_backup"] is True

        result = store.apply_migrations(applied_by="pytest:slim-worker-credentials")

        assert result["applied"] == ["0009_slim_worker_credentials", "0010_inference_tokens"]
        assert not [t for t in _SLIMMED_WORKER_CREDENTIAL_TABLES if _relation_exists(store, t)]
        assert not set(_SLIMMED_WORKER_CREDENTIAL_COLUMNS) & _worker_credential_columns(store)
        assert store.query_one("SELECT COUNT(*) AS n FROM worker_credentials")["n"] == 14
        resolved = WorkerCredentialPrincipalProvider(store).tokens()
        assert set(resolved) == {_token_hash(token) for token in tokens}
        for index, token in enumerate(tokens):
            principal = resolved[_token_hash(token)]
            assert principal["agent_id"] == "agent_%d" % index
            assert principal["worker_credential_state"] == "active"
            assert principal["worker_credential_version"] == 2
            assert "write" in principal["scopes"]
        verified = store.verify_schema()
        assert verified["status"] == "verified"
        assert verified["current_version"] == "0010_inference_tokens"
        assert "0009_slim_worker_credentials" in verified["proof"]["postconditions"]


def test_fresh_bootstrap_has_slim_worker_credentials(pg_dsn: str) -> None:
    from mac.services import ControlPlane
    from mac.worker_credentials import (
        WorkerCredentialLifecycle,
        WorkerCredentialPrincipalProvider,
        _token_hash,
    )

    with _fresh_store(pg_dsn) as store:
        store.initialize()
        assert not [t for t in _SLIMMED_WORKER_CREDENTIAL_TABLES if _relation_exists(store, t)]
        assert not set(_SLIMMED_WORKER_CREDENTIAL_COLUMNS) & _worker_credential_columns(store)
        verified = store.verify_schema()
        assert verified["status"] == "verified"
        assert verified["current_version"] == "0010_inference_tokens"

        cp = ControlPlane(store, secret_key="fresh-bootstrap-test-key-with-32-bytes")
        machine = cp.register_machine("fresh-host")
        agent = cp.register_agent(machine.id, "fresh-worker", agent_id="agent_fresh")
        lifecycle = WorkerCredentialLifecycle(store)
        issued = lifecycle.issue(agent.id)
        lifecycle.activate(agent.id, issued.record["id"])
        assert _token_hash(issued.token) in WorkerCredentialPrincipalProvider(store).tokens()


def test_upgrade_from_0009_adds_inference_tokens_and_keeps_worker_tokens(pg_dsn: str) -> None:
    """0010 only adds inference_tokens; the 0009 worker tokens keep resolving."""

    from mac.inference_tokens import InferenceTokenLifecycle, InferenceTokenPrincipalProvider
    from mac.worker_credentials import WorkerCredentialPrincipalProvider, _token_hash

    tokens = ["mac_worker_live_%d" % index for index in range(3)]
    with _fresh_store(pg_dsn) as store:
        store.apply_migrations(applied_by="pytest:through-0008", migrations=MIGRATIONS[:8])
        with store._pool.connection() as conn:
            _populate_live_worker_credentials(conn, tokens)
        store.apply_migrations(applied_by="pytest:through-0009", migrations=MIGRATIONS[:9])
        assert not _relation_exists(store, "inference_tokens")

        status = store.migration_status()
        assert status["pending"] == ["0010_inference_tokens"]

        result = store.apply_migrations(applied_by="pytest:inference-tokens")

        assert result["applied"] == ["0010_inference_tokens"]
        assert _relation_exists(store, "inference_tokens")
        resolved = WorkerCredentialPrincipalProvider(store).tokens()
        assert set(resolved) == {_token_hash(token) for token in tokens}
        issued = InferenceTokenLifecycle(store).mint("agent_1", task_id="task-1")
        principal = InferenceTokenPrincipalProvider(store).tokens()[_token_hash(issued.token)]
        assert principal["agent_id"] == "agent_1"
        assert principal["scopes"] == ["inference"]
        verified = store.verify_schema()
        assert verified["status"] == "verified"
        assert verified["current_version"] == "0010_inference_tokens"
        assert "0010_inference_tokens" in verified["proof"]["postconditions"]


def test_fresh_bootstrap_has_inference_tokens(pg_dsn: str) -> None:
    from datetime import datetime, timedelta, timezone

    from mac.inference_tokens import InferenceTokenLifecycle, InferenceTokenPrincipalProvider
    from mac.services import ControlPlane
    from mac.worker_credentials import _token_hash

    with _fresh_store(pg_dsn) as store:
        store.initialize()
        assert _relation_exists(store, "inference_tokens")
        assert store.verify_schema()["current_version"] == "0010_inference_tokens"

        cp = ControlPlane(store, secret_key="fresh-bootstrap-test-key-with-32-bytes")
        machine = cp.register_machine("fresh-host")
        agent = cp.register_agent(machine.id, "fresh-worker", agent_id="agent_fresh")
        lifecycle = InferenceTokenLifecycle(store)
        provider = InferenceTokenPrincipalProvider(store)
        issued = lifecycle.mint(agent.id, ttl_seconds=600)
        token_hash = _token_hash(issued.token)
        assert token_hash in provider.tokens()
        # Expiry alone ends it ...
        later = datetime.now(timezone.utc) + timedelta(seconds=601)
        assert token_hash not in provider.tokens(now=later)
        # ... and a later mint prunes rows a day past expiry.
        lifecycle.mint(agent.id, now=later + timedelta(days=2))
        assert (
            store.query_one(
                "SELECT COUNT(*) AS n FROM inference_tokens WHERE id = ?", (issued.id,)
            )["n"]
            == 0
        )
        # Revocation ends a token before its expiry.
        second = lifecycle.mint(agent.id)
        assert lifecycle.revoke(agent.id, second.id) is True
        assert _token_hash(second.token) not in provider.tokens()
        # Deleting the agent revokes whatever it still holds.
        third = lifecycle.mint(agent.id)
        cp.delete_agent(agent.id)
        assert _token_hash(third.token) not in provider.tokens()
        assert (
            store.query_one("SELECT revoked_at FROM inference_tokens WHERE id = ?", (third.id,))[
                "revoked_at"
            ]
            is not None
        )
