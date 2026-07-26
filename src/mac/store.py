"""Persistence store abstractions for the control plane.

Defines the store and connection protocols, the store error type, and the
SQLite-backed implementation plus factory used to persist MAC control-plane
state.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import (
    Any,
    ContextManager,
    Iterable,
    Iterator,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class StoreError(Exception):
    """Backend-neutral persistence error.

    SQLiteStore continues to surface ``sqlite3.Error`` subclasses directly
    so existing callers that catch ``sqlite3.IntegrityError`` keep working.
    Non-SQLite backends (e.g. PostgresStore) wrap their driver-native
    errors in ``StoreError``. Code that must handle either backend should
    catch ``(StoreError, sqlite3.Error)``.
    """


@runtime_checkable
class StoreConnection(Protocol):
    """Connection-like object yielded by ``Store.transaction()``."""

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any: ...


@runtime_checkable
class Store(Protocol):
    """Backend-agnostic persistence interface used by the control plane.

    Implementations accept SQL written in SQLite dialect. Non-SQLite
    backends translate placeholders and dialect-specific functions
    internally so service-layer SQL stays SQLite-shaped across the ~50
    service modules.
    """

    path: str

    def close(self) -> None: ...
    def transaction(self) -> ContextManager[StoreConnection]: ...
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any: ...
    def executemany(
        self, sql: str, params: Iterable[Sequence[Any]]
    ) -> Any: ...
    def query_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> Optional[Any]: ...
    def query_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list: ...


def make_store_from_env(
    sqlite_path: Optional[str] = None,
) -> "Store":
    """Backend-selecting store factory.

    Returns a `PostgresStore` when ``MAC_DATABASE_URL`` is set to a
    ``postgres://`` or ``postgresql://`` DSN; otherwise a `SQLiteStore`
    at the explicitly passed ``sqlite_path`` or ``MAC_DB``.

    There is deliberately no home-directory fallback. A process that owns a
    control plane must declare its durable authority; an operator client must
    never acquire a private ``~/.mac/mac.db`` merely by importing or starting
    MAC without a hub configuration.

    The Postgres backend auto-applies the bundled schema on first
    construction so a fresh CNPG cluster comes up ready; this server factory
    likewise constructs SQLite with schema initialization enabled.
    """
    role = os.environ.get("MAC_CONTROL_PLANE_ROLE", "").strip().lower()
    if role == "client":
        raise StoreError(
            "MAC_CONTROL_PLANE_ROLE=client cannot own a database; connect to the "
            "configured hub instead"
        )
    dsn = os.environ.get("MAC_DATABASE_URL", "").strip()
    if dsn:
        if not dsn.startswith(("postgres://", "postgresql://")):
            raise StoreError(
                "unsupported MAC_DATABASE_URL scheme; expected postgres:// or "
                "postgresql://"
            )
        from mac.store_postgres import PostgresStore

        pool_size = int(os.environ.get("MAC_PG_POOL_SIZE", "10") or "10")
        store = PostgresStore(dsn, pool_size=pool_size)
        store.initialize()
        return store
    path = sqlite_path or os.environ.get("MAC_DB", "").strip()
    if not path:
        raise StoreError(
            "control-plane database is not configured; set MAC_DATABASE_URL for "
            "PostgreSQL or MAC_DB to an explicit SQLite path. MAC no longer "
            "creates ~/.mac/mac.db implicitly."
        )
    return SQLiteStore(path)


class SQLiteStore:
    """Durable SQLite backing store for the control plane.

    Server startup uses the default ``initialize_schema=True`` to create and
    migrate the authority. Routine direct CLI access to an existing database
    passes ``False`` so a read does not acquire schema or journal-mode locks.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        initialize_schema: bool = True,
    ) -> None:
        if not path:
            raise StoreError(
                "SQLiteStore requires an explicit database path; pass a path "
                "directly or configure MAC_DB through make_store_from_env()"
            )
        self.path = path
        if path != ":memory:":
            database = Path(path).expanduser()
            if initialize_schema:
                database.parent.mkdir(parents=True, exist_ok=True)
            elif not database.is_file():
                raise StoreError(
                    "SQLite authority database does not exist: %s; run `mac --db %s init` "
                    "to initialize a standalone authority" % (database, database)
                )
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._transaction_state = threading.local()
        self._read_connections_lock = threading.Lock()
        self._read_connections: dict[int, sqlite3.Connection] = {}
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if initialize_schema and path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        if initialize_schema:
            self._initialize()

    def close(self) -> None:
        with self._read_connections_lock:
            read_connections = list(self._read_connections.values())
            self._read_connections.clear()
        for connection in read_connections:
            connection.close()
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._transaction_state.active = True
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                # Cancellation and interpreter-level exits inherit directly
                # from BaseException. If one crosses the context boundary,
                # leaving this shared connection in_transaction poisons every
                # later API write with "cannot start a transaction within a
                # transaction". Always unwind before releasing the RLock.
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            finally:
                self._transaction_state.active = False

    def _query_connection(self) -> Optional[sqlite3.Connection]:
        """Return a thread-local WAL reader outside explicit transactions.

        The hub has many background controllers but historically routed every
        read and write through one connection protected by ``self._lock``.
        One analytical read could therefore stall health-adjacent API reads,
        credential lookup, dispatch, CI monitoring, and repository
        reconciliation together. File-backed SQLite in WAL mode supports
        concurrent readers safely, so give each calling thread a query-only
        connection. Queries made from an explicit transaction continue using
        the writer connection so they observe that transaction's uncommitted
        state. In-memory stores necessarily retain the original single
        connection behavior.
        """

        if self.path == ":memory:" or bool(
            getattr(self._transaction_state, "active", False)
        ):
            return None
        thread_id = threading.get_ident()
        with self._read_connections_lock:
            connection = self._read_connections.get(thread_id)
            if connection is None:
                connection = sqlite3.connect(
                    self.path,
                    check_same_thread=False,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA query_only = ON")
                self._read_connections[thread_id] = connection
            return connection

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        # Autocommit semantics: SQLite commits a single statement on its own.
        # Inside an explicit transaction() block, statements run as part of that
        # transaction instead.
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, params)

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        connection = self._query_connection()
        if connection is not None:
            return connection.execute(sql, params).fetchone()
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list:
        connection = self._query_connection()
        if connection is not None:
            return connection.execute(sql, params).fetchall()
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # Durable pipeline resume cursors (task_repair_d771f872). Opaque,
    # bounded JSON documents keyed by a stable (scope, name). Used by the
    # work-package pipeline controller and the repository ref reconciler so a
    # hub restart resumes from its last bookmark instead of rescanning.
    PIPELINE_CURSOR_MAX_BYTES = 65536

    def set_pipeline_cursor(self, scope: str, name: str, value: Any) -> None:
        import json as _json

        scope_value = str(scope or "").strip()
        name_value = str(name or "").strip()
        if not scope_value or not name_value:
            raise ValueError("pipeline cursor scope and name are required")
        encoded = _json.dumps(value, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self.PIPELINE_CURSOR_MAX_BYTES:
            raise ValueError(
                "pipeline cursor value exceeds %d bytes"
                % self.PIPELINE_CURSOR_MAX_BYTES
            )
        now = _utcnow_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pipeline_cursors (scope, name, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope, name) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (scope_value, name_value, encoded, now),
            )

    def get_pipeline_cursor(self, scope: str, name: str, default: Any = None) -> Any:
        import json as _json

        scope_value = str(scope or "").strip()
        name_value = str(name or "").strip()
        if not scope_value or not name_value:
            return default
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM pipeline_cursors WHERE scope = ? AND name = ?",
                (scope_value, name_value),
            ).fetchone()
        if row is None:
            return default
        try:
            return _json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def _migrate_execution_cohort_route_contract(self) -> None:
        """Upgrade the preliminary cohort route CHECK without losing receipts.

        The telemetry schema was exercised before cut-over with a two-value
        route contract.  SQLite cannot alter a CHECK constraint in place, so
        repair that preliminary table once before the main schema/backfill runs.
        Only pre-v2 package assignments are reclassified; prospective v2/v3
        assignments retain their original identity.
        """
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("execution_cohort_assignments",),
        ).fetchone()
        if row is None or "unknown_managed_mode" in str(row["sql"] or ""):
            return

        has_finalizations = (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("work_package_publication_finalizations",),
            ).fetchone()
            is not None
        )
        receipt_exists = (
            "EXISTS (SELECT 1 FROM work_package_publication_finalizations AS "
            "finalization WHERE finalization.package_id = old.package_id)"
            if has_finalizations
            else "0"
        )
        receipt_id = (
            "COALESCE((SELECT finalization.id FROM "
            "work_package_publication_finalizations AS finalization "
            "WHERE finalization.package_id = old.package_id "
            "ORDER BY finalization.finalized_at, finalization.id LIMIT 1), '')"
            if has_finalizations
            else "''"
        )
        historical_package = (
            "old.package_id IS NOT NULL AND ("
            "old.assigned_by = 'schema-migration' OR "
            "CASE WHEN json_valid(old.detail) "
            "THEN COALESCE(json_extract(old.detail, '$.schema'), '') "
            "ELSE '' END NOT IN ("
            "'mac.execution_cohort.prospective.v2', "
            "'mac.execution_cohort.prospective.v3'))"
        )

        self._conn.execute("PRAGMA foreign_keys = OFF")
        self._conn.execute("PRAGMA legacy_alter_table = ON")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                """
                CREATE TABLE execution_cohort_assignments_v2 (
                    id TEXT PRIMARY KEY,
                    task_id TEXT UNIQUE,
                    package_id TEXT UNIQUE,
                    eligibility TEXT NOT NULL CHECK (
                        eligibility IN ('eligible', 'ineligible', 'unknown')
                    ),
                    treatment_route TEXT NOT NULL CHECK (
                        treatment_route IN (
                            'legacy_async', 'managed_synchronized',
                            'unknown_managed_mode'
                        )
                    ),
                    rollout_revision INTEGER NOT NULL CHECK (rollout_revision >= 0),
                    cohort_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    assigned_by TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    CHECK (task_id IS NOT NULL OR package_id IS NOT NULL)
                )
                """
            )
            self._conn.execute(
                f"""
                INSERT INTO execution_cohort_assignments_v2 (
                    id, task_id, package_id, eligibility, treatment_route,
                    rollout_revision, cohort_key, reason, detail, assigned_by,
                    assigned_at
                )
                SELECT
                    old.id,
                    old.task_id,
                    old.package_id,
                    CASE WHEN {historical_package}
                         THEN 'unknown' ELSE old.eligibility END,
                    CASE WHEN {historical_package}
                         THEN CASE WHEN {receipt_exists}
                              THEN 'managed_synchronized'
                              ELSE 'unknown_managed_mode' END
                         ELSE old.treatment_route END,
                    old.rollout_revision,
                    CASE WHEN {historical_package}
                         THEN CASE WHEN {receipt_exists}
                              THEN 'managed_receipted_pre_instrumentation'
                              ELSE 'managed_mode_unknown_pre_instrumentation' END
                         ELSE old.cohort_key END,
                    CASE WHEN {historical_package}
                         THEN CASE WHEN {receipt_exists}
                              THEN 'historical_synchronized_pipeline_receipt'
                              ELSE 'historical_package_mode_unproven' END
                         ELSE old.reason END,
                    CASE WHEN {historical_package}
                         THEN json_object(
                             'schema', 'mac.execution_cohort.backfill.v2',
                             'eligibility_source', 'unavailable',
                             'route_source', CASE WHEN {receipt_exists}
                                 THEN 'publication_finalization_receipt'
                                 ELSE 'unavailable' END,
                             'route_receipt_id', {receipt_id}
                         )
                         ELSE old.detail END,
                    old.assigned_by,
                    old.assigned_at
                FROM execution_cohort_assignments AS old
                """
            )
            self._conn.execute("DROP TABLE execution_cohort_assignments")
            self._conn.execute(
                "ALTER TABLE execution_cohort_assignments_v2 "
                "RENAME TO execution_cohort_assignments"
            )
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        finally:
            self._conn.execute("PRAGMA legacy_alter_table = OFF")
            self._conn.execute("PRAGMA foreign_keys = ON")

    def _repair_preliminary_package_cohorts(self) -> None:
        """One-time strict repair for stores that already recorded v2.

        A short-lived preliminary schema could write package assignments before
        the receipt-strict route contract existed, then record the v2 backfill
        marker.  Such a database no longer enters the guarded v2 scan.  Repair
        those package rows exactly once while the append-only triggers are
        transactionally suspended, and record a separate immutable receipt.
        """

        version = "execution_cohort_preliminary_package_repair_v3"
        if (
            self._conn.execute(
                "SELECT 1 FROM telemetry_data_migrations WHERE version = ?",
                (version,),
            ).fetchone()
            is not None
        ):
            return

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if (
                self._conn.execute(
                    "SELECT 1 FROM telemetry_data_migrations WHERE version = ?",
                    (version,),
                ).fetchone()
                is None
            ):
                self._conn.execute("DROP TRIGGER trg_execution_cohort_immutable")
                self._conn.execute("DROP TRIGGER trg_execution_cohort_no_delete")
                self._conn.execute(
                    """
                    UPDATE execution_cohort_assignments
                    SET eligibility = 'unknown',
                        treatment_route = CASE WHEN EXISTS (
                            SELECT 1
                            FROM work_package_publication_finalizations AS finalization
                            WHERE finalization.package_id =
                                  execution_cohort_assignments.package_id
                        ) THEN 'managed_synchronized'
                          ELSE 'unknown_managed_mode' END,
                        cohort_key = CASE WHEN EXISTS (
                            SELECT 1
                            FROM work_package_publication_finalizations AS finalization
                            WHERE finalization.package_id =
                                  execution_cohort_assignments.package_id
                        ) THEN 'managed_receipted_pre_instrumentation'
                          ELSE 'managed_mode_unknown_pre_instrumentation' END,
                        reason = CASE WHEN EXISTS (
                            SELECT 1
                            FROM work_package_publication_finalizations AS finalization
                            WHERE finalization.package_id =
                                  execution_cohort_assignments.package_id
                        ) THEN 'historical_synchronized_pipeline_receipt'
                          ELSE 'historical_package_mode_unproven' END,
                        detail = json_object(
                            'schema', 'mac.execution_cohort.backfill.v2',
                            'eligibility_source', 'unavailable',
                            'route_source', CASE WHEN EXISTS (
                                SELECT 1
                                FROM work_package_publication_finalizations AS finalization
                                WHERE finalization.package_id =
                                      execution_cohort_assignments.package_id
                            ) THEN 'publication_finalization_receipt'
                              ELSE 'unavailable' END,
                            'route_receipt_id', COALESCE((
                                SELECT finalization.id
                                FROM work_package_publication_finalizations AS finalization
                                WHERE finalization.package_id =
                                      execution_cohort_assignments.package_id
                                ORDER BY finalization.finalized_at, finalization.id
                                LIMIT 1
                            ), '')
                        )
                    WHERE package_id IS NOT NULL
                      AND CASE WHEN json_valid(detail)
                          THEN COALESCE(json_extract(detail, '$.schema'), '')
                          ELSE '' END NOT IN (
                              'mac.execution_cohort.prospective.v2',
                              'mac.execution_cohort.prospective.v3'
                          )
                    """
                )
                self._conn.execute(
                    """
                    INSERT INTO telemetry_data_migrations (
                        version, component, detail, applied_at
                    ) VALUES (?, 'execution_cohort_assignments', ?,
                              strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    """,
                    (
                        version,
                        '{"schema":"mac.telemetry_data_migration.v1",'
                        '"repair":"mac.execution_cohort.preliminary-package.v3"}',
                    ),
                )
                self._conn.execute(
                    """
                    CREATE TRIGGER trg_execution_cohort_immutable
                    BEFORE UPDATE ON execution_cohort_assignments
                    BEGIN
                        SELECT RAISE(
                            ABORT, 'execution cohort assignments are immutable'
                        );
                    END
                    """
                )
                self._conn.execute(
                    """
                    CREATE TRIGGER trg_execution_cohort_no_delete
                    BEFORE DELETE ON execution_cohort_assignments
                    BEGIN
                        SELECT RAISE(
                            ABORT, 'execution cohort assignments are append-only'
                        );
                    END
                    """
                )
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _migrate_station_controller_contract(self) -> None:
        """Add the controller station to preliminary telemetry databases."""

        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("work_package_station_attempts",),
        ).fetchone()
        table_sql = str(row["sql"] or "") if row is not None else ""
        if row is None or "'controller'" in table_sql:
            return
        self._conn.execute("PRAGMA foreign_keys = OFF")
        self._conn.execute("PRAGMA legacy_alter_table = ON")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                """
                CREATE TABLE work_package_station_attempts_v2 (
                    id TEXT PRIMARY KEY,
                    assignment_id TEXT NOT NULL
                        REFERENCES execution_cohort_assignments(id) ON DELETE RESTRICT,
                    package_id TEXT NOT NULL
                        REFERENCES work_packages(id) ON DELETE RESTRICT,
                    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
                    epoch INTEGER NOT NULL CHECK (epoch >= 1),
                    station TEXT NOT NULL CHECK (station IN (
                        'controller', 'admission', 'integration', 'certification',
                        'landing', 'finalization'
                    )),
                    operation TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                    attempted INTEGER NOT NULL CHECK (attempted IN (0, 1)),
                    pipeline_run_id TEXT NOT NULL DEFAULT '',
                    outcome_index INTEGER NOT NULL DEFAULT 0 CHECK (outcome_index >= 0),
                    batch_id TEXT NOT NULL DEFAULT '',
                    job_id TEXT NOT NULL DEFAULT '',
                    queued_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    queue_duration_ms INTEGER NOT NULL CHECK (queue_duration_ms >= 0),
                    execution_duration_ms INTEGER NOT NULL
                        CHECK (execution_duration_ms >= 0),
                    terminal_status TEXT NOT NULL CHECK (terminal_status IN (
                        'succeeded', 'failed', 'busy', 'held', 'stale',
                        'rejected', 'skipped'
                    )),
                    reason_code TEXT NOT NULL DEFAULT '',
                    failure_class TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    recorded_at TEXT NOT NULL,
                    UNIQUE (package_id, station, attempt_number),
                    UNIQUE (pipeline_run_id, outcome_index),
                    FOREIGN KEY (package_id, epoch, plan_version)
                        REFERENCES work_package_epochs(package_id, epoch, plan_version)
                        ON DELETE RESTRICT
                )
                """
            )
            self._conn.execute(
                "INSERT INTO work_package_station_attempts_v2 SELECT * "
                "FROM work_package_station_attempts"
            )
            self._conn.execute("DROP TABLE work_package_station_attempts")
            self._conn.execute(
                "ALTER TABLE work_package_station_attempts_v2 "
                "RENAME TO work_package_station_attempts"
            )
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        finally:
            self._conn.execute("PRAGMA legacy_alter_table = OFF")
            self._conn.execute("PRAGMA foreign_keys = ON")

    def _persona_instance_identity_violations(self) -> list[str]:
        """Return structural violations in the persona-instance schema."""

        tables = {
            str(row["name"])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        violations: list[str] = []
        if "hermes_instances" in tables:
            violations.append("legacy hermes_instances table is still present")
        if "persona_instances" not in tables:
            violations.append("persona_instances table is missing")
        if "platform_bindings" not in tables:
            violations.append("platform_bindings table is missing")
            return violations

        binding_columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(platform_bindings)")
        }
        if "hermes_instance_id" in binding_columns:
            violations.append(
                "legacy platform_bindings.hermes_instance_id column is still present"
            )
        if "persona_instance_id" not in binding_columns:
            violations.append(
                "platform_bindings.persona_instance_id column is missing"
            )

        instance_foreign_keys = [
            row
            for row in self._conn.execute("PRAGMA foreign_key_list(platform_bindings)")
            if str(row["from"]) in {
                "hermes_instance_id",
                "persona_instance_id",
            }
        ]
        if len(instance_foreign_keys) != 1:
            violations.append(
                "platform_bindings must have exactly one persona-instance foreign key"
            )
        elif (
            str(instance_foreign_keys[0]["from"]) != "persona_instance_id"
            or str(instance_foreign_keys[0]["table"]) != "persona_instances"
        ):
            violations.append(
                "platform_bindings persona-instance foreign key does not reference "
                "persona_instances(id)"
            )

        legacy_schema_objects = [
            str(row["name"])
            for row in self._conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE sql IS NOT NULL
                  AND lower(sql) LIKE '%hermes_instances%'
                ORDER BY name
                """
            )
        ]
        if legacy_schema_objects:
            violations.append(
                "legacy hermes_instances references remain in schema objects: %s"
                % ", ".join(legacy_schema_objects)
            )
        return violations

    def _verify_persona_instance_identity(self) -> None:
        """Fail closed unless the migrated schema and database are sound."""

        violations = self._persona_instance_identity_violations()
        if violations:
            raise StoreError(
                "persona-instance schema verification failed: %s"
                % "; ".join(violations)
            )

        integrity_rows = [
            str(row[0]) for row in self._conn.execute("PRAGMA integrity_check")
        ]
        if integrity_rows != ["ok"]:
            raise StoreError(
                "SQLite integrity_check failed during persona-instance migration: %s"
                % "; ".join(integrity_rows)
            )

        foreign_key_rows = list(self._conn.execute("PRAGMA foreign_key_check"))
        if foreign_key_rows:
            first = foreign_key_rows[0]
            raise StoreError(
                "SQLite foreign_key_check failed during persona-instance migration: "
                "table=%s rowid=%s parent=%s fk=%s"
                % tuple(first)
            )

    def _record_persona_instance_identity_receipt(
        self,
        *,
        version: str,
        origin: str,
        repair: bool = False,
    ) -> None:
        detail = (
            '{"schema":"mac.schema_migration.v1",'
            '"rename":"hermes_instances->persona_instances",'
            '"column_rename":"platform_bindings.hermes_instance_id'
            '->persona_instance_id","origin":"%s"%s}'
            % (
                origin,
                ',"repair":"legacy-rename-foreign-key"' if repair else "",
            )
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO schema_migration_receipts (
                version, component, detail, applied_at
            ) VALUES (?, 'persona_instances', ?,
                      strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """,
            (version, detail),
        )

    def _migrate_persona_instance_identity(self) -> bool:
        """Rename the live-Hermes identity tables to the persona-neutral name.

        ``hermes_instances`` became ``persona_instances`` and the
        ``platform_bindings.hermes_instance_id`` foreign key became
        ``persona_instance_id`` as part of the runtime-neutral PersonaInstance
        model. This runs once, before the idempotent ``CREATE TABLE IF NOT
        EXISTS`` schema so an old-schema database is upgraded in place (rather
        than leaving the old table orphaned beside a fresh empty one). Every
        row and every foreign-key relationship is preserved.

        The rename is recorded as an append-only, immutable receipt in
        ``schema_migration_receipts`` so startup can prove the one-time
        migration already ran without re-inspecting the catalog. This temporary
        read migration exists for this release only; there is no long-term
        dual-name support.
        """
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migration_receipts (
                version TEXT PRIMARY KEY,
                component TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '{}',
                applied_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_schema_migration_receipt_immutable
            BEFORE UPDATE ON schema_migration_receipts
            BEGIN
                SELECT RAISE(ABORT, 'schema migration receipts are immutable');
            END
            """
        )
        self._conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_schema_migration_receipt_no_delete
            BEFORE DELETE ON schema_migration_receipts
            BEGIN
                SELECT RAISE(ABORT, 'schema migration receipts are append-only');
            END
            """
        )

        version = "mac.persona_instance_identity.v1"
        repair_version = "mac.persona_instance_identity_fk_repair.v1"
        already = self._conn.execute(
            "SELECT 1 FROM schema_migration_receipts WHERE version = ?",
            (version,),
        ).fetchone()

        has_old_instances = (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("hermes_instances",),
            ).fetchone()
            is not None
        )
        has_new_instances = (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("persona_instances",),
            ).fetchone()
            is not None
        )
        if not has_old_instances and not has_new_instances:
            # The main idempotent schema below creates both new tables. Defer
            # verification and the receipt until that schema actually exists.
            return True
        if has_old_instances and has_new_instances:
            raise StoreError(
                "persona-instance schema is ambiguous: hermes_instances and "
                "persona_instances both exist"
            )

        violations = (
            self._persona_instance_identity_violations()
            if has_new_instances
            else ["legacy hermes_instances table is still present"]
        )
        if not violations:
            if already is None:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._verify_persona_instance_identity()
                    self._record_persona_instance_identity_receipt(
                        version=version,
                        origin="existing-new-schema",
                    )
                    self._conn.execute("COMMIT")
                except BaseException:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise
            return False

        foreign_keys = int(
            self._conn.execute("PRAGMA foreign_keys").fetchone()[0]
        )
        legacy_alter_table = int(
            self._conn.execute("PRAGMA legacy_alter_table").fetchone()[0]
        )
        self._conn.execute("PRAGMA foreign_keys = OFF")
        # SQLite 3.26+ rewrites dependent foreign keys during table renames
        # only with legacy_alter_table disabled. The previous ON setting was
        # the root cause of references being stranded on hermes_instances.
        self._conn.execute("PRAGMA legacy_alter_table = OFF")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if has_old_instances and not has_new_instances:
                self._conn.execute(
                    "ALTER TABLE hermes_instances RENAME TO persona_instances"
                )
                self._conn.execute(
                    "DROP INDEX IF EXISTS idx_hermes_instances_tenant"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_persona_instances_tenant "
                    "ON persona_instances (tenant_id)"
                )
            elif has_new_instances:
                # Repair the released v1 migration, whose immutable receipt
                # could claim success while platform_bindings still referenced
                # the vanished hermes_instances table. Renaming through the
                # exact legacy name makes modern SQLite rewrite every dependent
                # reference before restoring the persona-neutral name.
                self._conn.execute(
                    "ALTER TABLE persona_instances RENAME TO hermes_instances"
                )
                self._conn.execute(
                    "ALTER TABLE hermes_instances RENAME TO persona_instances"
                )

            binding_columns = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(platform_bindings)"
                )
            }
            if (
                binding_columns
                and "hermes_instance_id" in binding_columns
                and "persona_instance_id" not in binding_columns
            ):
                self._conn.execute(
                    "ALTER TABLE platform_bindings "
                    "RENAME COLUMN hermes_instance_id TO persona_instance_id"
                )
                self._conn.execute(
                    "DROP INDEX IF EXISTS idx_platform_bindings_instance"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_platform_bindings_instance "
                    "ON platform_bindings (persona_instance_id)"
                )

            self._conn.execute("DROP INDEX IF EXISTS idx_hermes_instances_tenant")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_persona_instances_tenant "
                "ON persona_instances (tenant_id)"
            )
            self._conn.execute("DROP INDEX IF EXISTS idx_platform_bindings_instance")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_platform_bindings_instance "
                "ON platform_bindings (persona_instance_id)"
            )

            self._verify_persona_instance_identity()
            if already is None:
                self._record_persona_instance_identity_receipt(
                    version=version,
                    origin="old-schema",
                )
            else:
                self._record_persona_instance_identity_receipt(
                    version=repair_version,
                    origin="false-v1-receipt",
                    repair=True,
                )
            self._conn.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        finally:
            self._conn.execute(
                "PRAGMA legacy_alter_table = %s" % legacy_alter_table
            )
            self._conn.execute("PRAGMA foreign_keys = %s" % foreign_keys)
        return False

    def _initialize(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._migrate_execution_cohort_route_contract()
            self._migrate_station_controller_contract()
            fresh_persona_identity = self._migrate_persona_instance_identity()
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    handle TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, handle)
                );
                CREATE INDEX IF NOT EXISTS idx_users_tenant
                    ON users (tenant_id);

                CREATE TABLE IF NOT EXISTS personas (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    soul_ref TEXT NOT NULL,
                    memory_scope TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_personas_tenant
                    ON personas (tenant_id);

                CREATE TABLE IF NOT EXISTS persona_instances (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    persona_id TEXT REFERENCES personas(id) ON DELETE SET NULL,
                    home_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(tenant_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_persona_instances_tenant
                    ON persona_instances (tenant_id);

                CREATE TABLE IF NOT EXISTS platform_bindings (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    persona_instance_id TEXT NOT NULL REFERENCES persona_instances(id) ON DELETE CASCADE,
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, platform, external_id)
                );
                CREATE INDEX IF NOT EXISTS idx_platform_bindings_instance
                    ON platform_bindings (persona_instance_id);

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    project TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    required_capabilities TEXT NOT NULL,
                    dependencies TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    owner_agent_id TEXT,
                    lease_id TEXT,
                    leased_until TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    started_at TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    human_assignees TEXT,
                    created_by_human TEXT,
                    idempotency_key TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_state_priority
                    ON tasks (state, priority DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_review_queue
                    ON tasks (priority DESC, created_at, id)
                    WHERE state IN ('needs_review', 'reviewing');
                CREATE INDEX IF NOT EXISTS idx_tasks_state_updated
                    ON tasks (state, updated_at, id);
                CREATE INDEX IF NOT EXISTS idx_tasks_owner
                    ON tasks (owner_agent_id);
                -- mac-1hnt: enforce the task state machine at the DB
                -- layer. A SQLite CHECK constraint can't be added to
                -- an existing table, so use a trigger that rejects
                -- INSERTs/UPDATEs with a state outside the enum. The
                -- Python ``validate_transition`` still handles the
                -- richer "from → to" rule; this trigger is the
                -- belt-and-braces against bare UPDATEs and bugs.
                CREATE TRIGGER IF NOT EXISTS trg_tasks_state_enum_ins
                BEFORE INSERT ON tasks
                FOR EACH ROW
                WHEN NEW.state NOT IN (
                    'open', 'waiting', 'blocked', 'claimed', 'running',
                    'needs_review', 'reviewing', 'completed',
                    'failed', 'cancelled'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid task state');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_tasks_state_enum_upd
                BEFORE UPDATE OF state ON tasks
                FOR EACH ROW
                WHEN NEW.state NOT IN (
                    'open', 'waiting', 'blocked', 'claimed', 'running',
                    'needs_review', 'reviewing', 'completed',
                    'failed', 'cancelled'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid task state');
                END;

                CREATE TABLE IF NOT EXISTS task_history (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_history_task_created
                    ON task_history (task_id, created_at);
                -- mac-ykkc: index event_type so debug/audit queries by action
                -- (e.g. counting task.review_claimed) are not table scans.
                CREATE INDEX IF NOT EXISTS idx_task_history_event_type
                    ON task_history (event_type, task_id);

                CREATE TABLE IF NOT EXISTS task_transition_outbox (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    detail TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    processed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_task_transition_outbox_status
                    ON task_transition_outbox (status, created_at);
                CREATE INDEX IF NOT EXISTS idx_task_transition_outbox_task
                    ON task_transition_outbox (task_id, created_at);

                CREATE TABLE IF NOT EXISTS reconciliation_state (
                    name TEXT PRIMARY KEY,
                    cursor TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reconciliation_state_lease
                    ON reconciliation_state (lease_expires_at, name);

                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    checksum TEXT,
                    metadata TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_task
                    ON evidence (task_id);

                CREATE TABLE IF NOT EXISTS evidence_artifacts (
                    id TEXT PRIMARY KEY,
                    evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    encoding TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    content_base64 TEXT NOT NULL,
                    content_uri TEXT NOT NULL DEFAULT '',
                    truncated INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_evidence
                    ON evidence_artifacts (evidence_id, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_task
                    ON evidence_artifacts (task_id, created_at, id);

                CREATE TABLE IF NOT EXISTS leases (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    -- PR2c (spec §6.3, Option B): dispatcher (lease owner)
                    -- may delegate lifecycle authorship to the role agent
                    -- spawned in the task Job. NULL = no delegation; the
                    -- owner is the sole authoriser.
                    delegated_agent_id TEXT,
                    expiry_finalizer_token TEXT,
                    expiry_finalizer_claimed_at TEXT,
                    expiry_finalized_at TEXT,
                    expiry_finalization_decision TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_leases_task_status
                    ON leases (task_id, status);
                CREATE INDEX IF NOT EXISTS idx_leases_agent_status
                    ON leases (agent_id, status);
                CREATE INDEX IF NOT EXISTS idx_leases_status_expiry
                    ON leases (status, expires_at, id);
                -- mac-x5el: enforce "at most one ACTIVE lease per task"
                -- at the DB layer so a Python bug or a manual UPDATE
                -- cannot produce duplicate active leases that confuse
                -- claim/release/expire.
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_leases_active_per_task
                    ON leases (task_id) WHERE status = 'active';

                -- media-01 service-role election: desired media services + the
                -- leased holds capable hosts claim (mirrors tasks+leases).
                CREATE TABLE IF NOT EXISTS service_roles (
                    id TEXT PRIMARY KEY,
                    op TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    model_id TEXT,
                    required_capabilities TEXT NOT NULL DEFAULT '[]',
                    hardware_requirements TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(slug, tenant_id)
                );
                CREATE TABLE IF NOT EXISTS service_claims (
                    id TEXT PRIMARY KEY,
                    service_role_id TEXT NOT NULL REFERENCES service_roles(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_service_claims_role_status
                    ON service_claims (service_role_id, status);
                CREATE INDEX IF NOT EXISTS idx_service_claims_agent_status
                    ON service_claims (agent_id, status);
                -- Pool model: a host holds an op at most once; multiple hosts may
                -- hold the same op. Split-brain guard at the DB layer.
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_service_claims_active_per_role_agent
                    ON service_claims (service_role_id, agent_id) WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS machines (
                    id TEXT PRIMARY KEY,
                    hostname TEXT NOT NULL,
                    labels TEXT NOT NULL,
                    resources TEXT NOT NULL,
                    trusted INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    machine_id TEXT NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    instance_kind TEXT NOT NULL DEFAULT 'static'
                        CHECK (instance_kind IN ('static', 'fungible')),
                    capabilities TEXT NOT NULL,
                    resources TEXT NOT NULL,
                    status TEXT NOT NULL,
                    health_status TEXT NOT NULL,
                    current_task_id TEXT,
                    running_digest TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agents_status_health
                    ON agents (status, health_status);

                -- Hub-authoritative per-worker identities. Bearer material is
                -- never stored: token_hash is the only credential secret
                -- derivative and audit rows deliberately omit it. Keeping
                -- this state beside agents makes every API replica and the
                -- in-transaction package claim gate observe one authority.
                CREATE TABLE IF NOT EXISTS worker_credentials (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
                    fleet TEXT NOT NULL DEFAULT '',
                    credential_version INTEGER NOT NULL CHECK (credential_version >= 1),
                    token_hash TEXT NOT NULL UNIQUE,
                    token_fingerprint TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    environment TEXT NOT NULL CHECK (environment IN ('vm', 'k8s')),
                    expected_source_commit TEXT NOT NULL DEFAULT '',
                    expected_runtime_digest TEXT NOT NULL DEFAULT '',
                    required_capabilities TEXT NOT NULL DEFAULT '[]',
                    package_capable INTEGER NOT NULL DEFAULT 0 CHECK (package_capable IN (0, 1)),
                    state TEXT NOT NULL CHECK (
                        state IN ('pending_install', 'active', 'superseded', 'revoked')
                    ),
                    destination TEXT NOT NULL DEFAULT '',
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    activated_at TEXT,
                    revoked_at TEXT,
                    superseded_by TEXT REFERENCES worker_credentials(id) ON DELETE RESTRICT,
                    created_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(agent_id, credential_version)
                );
                CREATE INDEX IF NOT EXISTS idx_worker_credentials_agent_state
                    ON worker_credentials (agent_id, state, credential_version DESC);
                CREATE INDEX IF NOT EXISTS idx_worker_credentials_expiry
                    ON worker_credentials (expires_at, state);

                CREATE TABLE IF NOT EXISTS worker_credential_events (
                    id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL REFERENCES worker_credentials(id) ON DELETE RESTRICT,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_worker_credential_events_agent_created
                    ON worker_credential_events (agent_id, created_at, id);

                -- Singleton, database-backed rollout authority. A local file
                -- cannot coordinate multiple API replicas, so the compatibility
                -- to enforced flip and its reviewed membership live here.
                CREATE TABLE IF NOT EXISTS worker_credential_policy_state (
                    singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'fleet'),
                    mode TEXT NOT NULL CHECK (mode IN ('compatibility', 'enforced')),
                    inventory_digest TEXT,
                    ready_agent_ids TEXT NOT NULL DEFAULT '[]',
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                -- Stable physical authority for the hub database. Runtime
                -- code inserts the random UUID once with ON CONFLICT and all
                -- replicas subsequently read the winning singleton.
                CREATE TABLE IF NOT EXISTS hub_authority_identity (
                    singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'hub'),
                    authority_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                -- Durable hub participant in a synchronized fleet cutover.
                -- The canonical identity payload and every receipt are
                -- secret-free. Symmetric attestation candidates live in the
                -- separate encrypted staging table below and are deleted on
                -- either commit or abort.
                CREATE TABLE IF NOT EXISTS fleet_release_epochs (
                    epoch_id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    identity_sha256 TEXT NOT NULL UNIQUE,
                    identity_payload TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('open', 'proved', 'committed', 'aborted')
                    ),
                    proof_sha256 TEXT,
                    successor_hold_reason TEXT,
                    desired_policy_mode TEXT CHECK (
                        desired_policy_mode IS NULL OR
                        desired_policy_mode IN ('compatibility', 'enforced')
                    ),
                    policy_snapshot TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    prepared_at TEXT NOT NULL,
                    proved_at TEXT,
                    committed_at TEXT,
                    aborted_at TEXT,
                    abort_reason TEXT,
                    abort_disposition TEXT
                );

                CREATE TABLE IF NOT EXISTS fleet_release_epoch_agents (
                    epoch_id TEXT NOT NULL REFERENCES fleet_release_epochs(epoch_id)
                        ON DELETE RESTRICT,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                    open_state INTEGER NOT NULL DEFAULT 1 CHECK (open_state IN (0, 1)),
                    prior_dispatch_hold INTEGER NOT NULL CHECK (
                        prior_dispatch_hold IN (0, 1)
                    ),
                    prior_hold_reason TEXT,
                    prior_hold_at TEXT,
                    epoch_hold_reason TEXT NOT NULL,
                    epoch_hold_at TEXT NOT NULL,
                    prior_active_service_claim_ids TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    baseline_seen TEXT NOT NULL,
                    principal_id TEXT NOT NULL REFERENCES worker_credentials(id)
                        ON DELETE RESTRICT,
                    principal_version INTEGER NOT NULL CHECK (principal_version >= 1),
                    principal_fingerprint TEXT NOT NULL,
                    install_receipt TEXT,
                    install_receipt_sha256 TEXT,
                    prior_live_principal_ids TEXT NOT NULL,
                    prior_attestation_ciphertext_sha256 TEXT NOT NULL,
                    attestation_candidate_fingerprint TEXT,
                    attestation_proof TEXT,
                    attestation_proof_sha256 TEXT,
                    report_executor_action TEXT NOT NULL CHECK (
                        report_executor_action IN ('preserve', 'approve', 'revoke')
                    ),
                    prior_report_executor_projection_sha256 TEXT NOT NULL,
                    report_executor_attestation TEXT,
                    report_executor_startup_timestamp TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (epoch_id, agent_id),
                    UNIQUE (epoch_id, ordinal)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_release_open_agent
                    ON fleet_release_epoch_agents (agent_id) WHERE open_state = 1;
                CREATE INDEX IF NOT EXISTS idx_fleet_release_epoch_agents_epoch
                    ON fleet_release_epoch_agents (epoch_id, ordinal);

                CREATE TABLE IF NOT EXISTS fleet_release_attestation_candidates (
                    epoch_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    key_ciphertext TEXT NOT NULL,
                    key_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (epoch_id, agent_id),
                    FOREIGN KEY (epoch_id, agent_id)
                        REFERENCES fleet_release_epoch_agents(epoch_id, agent_id)
                        ON DELETE RESTRICT
                );

                -- One-way authority for ordinary atomic task publication. The
                -- row is absent before rollout and is inserted exactly once
                -- after the reviewed package-worker policy and controller
                -- runtime are ready. It is intentionally not reversible:
                -- later fleet/config drift may hold managed work, but cannot
                -- silently restore the legacy publication path.
                CREATE TABLE IF NOT EXISTS managed_task_publication_rollout (
                    singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'fleet'),
                    revision INTEGER NOT NULL CHECK (revision = 1),
                    crossed_by TEXT NOT NULL,
                    crossed_at TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '{}'
                );

                -- HTTP/CLI create retries reserve an identity before task or
                -- package admission. Only digests are stored; raw caller keys
                -- never enter the ledger. A reservation may outlive a failed
                -- request so the same key can never name a different intent.
                CREATE TABLE IF NOT EXISTS task_create_idempotency (
                    scope_digest TEXT NOT NULL,
                    key_digest TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scope_digest, key_digest)
                );

                -- Durable crash diagnosis. ``agent_crash_reports`` is the
                -- deduplicated incident keyed by a server-computed
                -- revision+stack fingerprint; ``agent_crash_occurrences``
                -- preserves every supervisor-observed failure, including
                -- reports spooled while the hub was unavailable.
                CREATE TABLE IF NOT EXISTS agent_crash_reports (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    process_name TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    stack_signature TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    occurrence_count INTEGER NOT NULL DEFAULT 0,
                    repair_attempt_count INTEGER NOT NULL DEFAULT 0,
                    affected_agent_ids TEXT NOT NULL DEFAULT '[]',
                    repair_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_crash_reports_status_last_seen
                    ON agent_crash_reports (status, last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_agent_crash_reports_repair_task
                    ON agent_crash_reports (repair_task_id);

                CREATE TABLE IF NOT EXISTS agent_crash_occurrences (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    report_id TEXT NOT NULL REFERENCES agent_crash_reports(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    observed_at TEXT NOT NULL,
                    supervisor TEXT NOT NULL,
                    process_name TEXT NOT NULL,
                    pid INTEGER,
                    exit_code INTEGER,
                    signal INTEGER,
                    reason TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    tree_sha TEXT NOT NULL,
                    task_id TEXT,
                    lease_id TEXT,
                    stack_trace TEXT NOT NULL,
                    stderr_tail TEXT NOT NULL,
                    core_reference TEXT NOT NULL,
                    core_metadata TEXT NOT NULL DEFAULT '{}',
                    resource_snapshot TEXT NOT NULL DEFAULT '{}',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_crash_occurrences_report
                    ON agent_crash_occurrences (report_id, observed_at);
                CREATE INDEX IF NOT EXISTS idx_agent_crash_occurrences_agent
                    ON agent_crash_occurrences (agent_id, observed_at);

                CREATE TABLE IF NOT EXISTS fleets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    tenant_id TEXT REFERENCES tenants(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fleets_status_name
                    ON fleets (status, name);
                CREATE INDEX IF NOT EXISTS idx_fleets_tenant
                    ON fleets (tenant_id);

                CREATE TABLE IF NOT EXISTS fleet_agents (
                    fleet_id TEXT NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (fleet_id, agent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_agents_agent
                    ON fleet_agents (agent_id);

                CREATE TABLE IF NOT EXISTS fleet_agent_observations (
                    fleet_id TEXT NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (fleet_id, agent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_agent_observations_agent
                    ON fleet_agent_observations (agent_id);
                CREATE INDEX IF NOT EXISTS idx_fleet_agent_observations_last_seen
                    ON fleet_agent_observations (last_seen_at);

                CREATE TABLE IF NOT EXISTS fleet_events (
                    id TEXT PRIMARY KEY,
                    fleet_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_events_fleet_created
                    ON fleet_events (fleet_id, created_at);

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    sender_agent_id TEXT NOT NULL,
                    recipient_agent_id TEXT,
                    task_id TEXT,
                    message_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_messages_recipient_status
                    ON messages (recipient_agent_id, status);

                CREATE TABLE IF NOT EXISTS agentbus_streams (
                    id TEXT PRIMARY KEY,
                    sender_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    recipient_agent_id TEXT REFERENCES agents(id) ON DELETE CASCADE,
                    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    topic TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    headers TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_agentbus_streams_recipient_status
                    ON agentbus_streams (recipient_agent_id, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_agentbus_streams_sender_status
                    ON agentbus_streams (sender_agent_id, status, updated_at);

                CREATE TABLE IF NOT EXISTS agentbus_chunks (
                    id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL REFERENCES agentbus_streams(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    sender_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    content_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_encoding TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(stream_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_agentbus_chunks_stream_sequence
                    ON agentbus_chunks (stream_id, sequence);

                CREATE TABLE IF NOT EXISTS observability_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    layer TEXT NOT NULL,
                    source TEXT NOT NULL,
                    level TEXT NOT NULL,
                    name TEXT NOT NULL,
                    subject_type TEXT,
                    subject_id TEXT,
                    value REAL,
                    unit TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observability_events_created
                    ON observability_events (created_at, sequence);
                CREATE INDEX IF NOT EXISTS idx_observability_events_kind_layer
                    ON observability_events (kind, layer, created_at);
                CREATE INDEX IF NOT EXISTS idx_observability_events_name_created
                    ON observability_events (name, created_at);
                CREATE INDEX IF NOT EXISTS idx_observability_events_subject_sequence
                    ON observability_events (
                        kind, name, subject_type, subject_id, sequence DESC
                    );

                CREATE TABLE IF NOT EXISTS operator_notifications (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    subject_type TEXT,
                    subject_id TEXT,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    channels TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_operator_notifications_status_created
                    ON operator_notifications (status, created_at);
                CREATE INDEX IF NOT EXISTS idx_operator_notifications_subject
                    ON operator_notifications (subject_type, subject_id, created_at);

                CREATE TABLE IF NOT EXISTS notifier_channels (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    channel_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    event_types TEXT NOT NULL DEFAULT '[]',
                    target TEXT NOT NULL DEFAULT '{}',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notifier_channels_type_enabled
                    ON notifier_channels (channel_type, enabled);

                -- Human-facing identities are logical fleet resources.  They
                -- are deliberately independent of workers and of the retired
                -- Hermes-instance identity model so one stable "hive" can
                -- represent any number of internal agents.
                CREATE TABLE IF NOT EXISTS communication_identities (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_communication_identity_default
                    ON communication_identities (is_default) WHERE is_default = 1;

                CREATE TABLE IF NOT EXISTS communication_accounts (
                    id TEXT PRIMARY KEY,
                    identity_id TEXT NOT NULL REFERENCES communication_identities(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    credential_refs TEXT NOT NULL DEFAULT '{}',
                    config TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(identity_id, channel, account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_communication_accounts_channel
                    ON communication_accounts (channel, enabled);

                CREATE TABLE IF NOT EXISTS representation_bindings (
                    id TEXT PRIMARY KEY,
                    subject_kind TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    identity_id TEXT REFERENCES communication_identities(id) ON DELETE CASCADE,
                    mode TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(subject_kind, subject_id)
                );
                CREATE INDEX IF NOT EXISTS idx_representation_bindings_identity
                    ON representation_bindings (identity_id, enabled);

                CREATE TABLE IF NOT EXISTS gateway_identity_leases (
                    id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL UNIQUE REFERENCES communication_accounts(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    fencing_token TEXT NOT NULL UNIQUE,
                    leased_until TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_gateway_identity_leases_agent
                    ON gateway_identity_leases (agent_id, leased_until);

                CREATE TABLE IF NOT EXISTS human_message_deliveries (
                    id TEXT PRIMARY KEY,
                    identity_id TEXT NOT NULL REFERENCES communication_identities(id) ON DELETE RESTRICT,
                    account_id TEXT NOT NULL REFERENCES communication_accounts(id) ON DELETE RESTRICT,
                    channel TEXT,
                    target TEXT NOT NULL,
                    body TEXT NOT NULL,
                    origin_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
                    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    delivery_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
                    delivery_lease_id TEXT,
                    leased_until TEXT,
                    provider_message_id TEXT,
                    last_error TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_human_message_deliveries_status
                    ON human_message_deliveries (status, created_at);
                CREATE INDEX IF NOT EXISTS idx_human_message_deliveries_identity
                    ON human_message_deliveries (identity_id, created_at);

                CREATE TABLE IF NOT EXISTS command_audit (
                    id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    argv TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    task_id TEXT,
                    lease_id TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_ms REAL,
                    returncode INTEGER,
                    stdout_sha256 TEXT,
                    stderr_sha256 TEXT,
                    stdout_bytes INTEGER,
                    stderr_bytes INTEGER,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_command_audit_created
                    ON command_audit (created_at, id);
                CREATE INDEX IF NOT EXISTS idx_command_audit_agent_created
                    ON command_audit (agent_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_command_audit_task_created
                    ON command_audit (task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_command_audit_command
                    ON command_audit (command_id, created_at);

                CREATE TABLE IF NOT EXISTS action_events (
                    event_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    agent_id TEXT,
                    hermes_instance_id TEXT,
                    task_id TEXT,
                    session_id TEXT,
                    sandbox_id TEXT,
                    actor TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_name TEXT NOT NULL,
                    subject_type TEXT,
                    subject_id TEXT,
                    outcome TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    policy_id TEXT,
                    policy_version INTEGER,
                    command_id TEXT,
                    parent_event_id TEXT,
                    attributes TEXT NOT NULL,
                    redaction_state TEXT NOT NULL
                );
                -- AMANALAP: action_events is the highest-write table in the
                -- system.  Each secondary index is another B-tree written on
                -- every insert (the write amplification that, with unbounded
                -- growth, wedged the hub).  Retention now bounds the table, so
                -- the rare admin/dashboard filters (agent/session/sandbox/
                -- policy/type) can scan a small window instead of each paying a
                -- permanent write-amplification tax.  Keep only the two indexes
                -- that serve common queries: time-window (+ORDER BY) and
                -- per-task drill-down.  Drop the other five from existing DBs.
                CREATE INDEX IF NOT EXISTS idx_action_events_timestamp
                    ON action_events (timestamp, event_id);
                CREATE INDEX IF NOT EXISTS idx_action_events_task_timestamp
                    ON action_events (task_id, timestamp);
                DROP INDEX IF EXISTS idx_action_events_agent_timestamp;
                DROP INDEX IF EXISTS idx_action_events_session_timestamp;
                DROP INDEX IF EXISTS idx_action_events_sandbox_timestamp;
                DROP INDEX IF EXISTS idx_action_events_policy_timestamp;
                DROP INDEX IF EXISTS idx_action_events_type_outcome;

                CREATE TABLE IF NOT EXISTS openshell_policies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    policy_text TEXT NOT NULL,
                    parsed_metadata TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    checksum TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_openshell_policies_active_name
                    ON openshell_policies (active, name);

                CREATE TABLE IF NOT EXISTS openshell_policy_versions (
                    id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL REFERENCES openshell_policies(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    policy_text TEXT NOT NULL,
                    parsed_metadata TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(policy_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_openshell_policy_versions_policy
                    ON openshell_policy_versions (policy_id, version);

                CREATE TABLE IF NOT EXISTS openshell_policy_assignments (
                    id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL REFERENCES openshell_policies(id) ON DELETE CASCADE,
                    policy_version INTEGER NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_openshell_policy_assignments_target
                    ON openshell_policy_assignments (target_type, target_id, active);
                CREATE INDEX IF NOT EXISTS idx_openshell_policy_assignments_policy
                    ON openshell_policy_assignments (policy_id, active);

                CREATE TABLE IF NOT EXISTS openshell_agent_status (
                    agent_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    required INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    sandbox_id TEXT,
                    policy_id TEXT,
                    policy_version INTEGER,
                    checksum TEXT,
                    supervisor_pid INTEGER,
                    detail TEXT NOT NULL,
                    reported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_openshell_agent_status_status
                    ON openshell_agent_status (status, reported_at);

                -- Versioned fleet directives are hub-owned policy data.  The
                -- immutable version rows are separated from mutable heads,
                -- approvals, activation epochs, and agent acknowledgements so
                -- every dispatch decision can be reproduced exactly.
                CREATE TABLE IF NOT EXISTS fleet_directives (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    current_version INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    reserved INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_directives_state_name
                    ON fleet_directives (state, name);

                CREATE TABLE IF NOT EXISTS fleet_directive_versions (
                    id TEXT PRIMARY KEY,
                    directive_id TEXT NOT NULL REFERENCES fleet_directives(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    document TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(directive_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_directive_versions_directive
                    ON fleet_directive_versions (directive_id, version);
                CREATE TRIGGER IF NOT EXISTS trg_fleet_directive_versions_immutable_update
                BEFORE UPDATE ON fleet_directive_versions
                BEGIN
                    SELECT RAISE(ABORT, 'fleet directive versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_fleet_directive_versions_immutable_delete
                BEFORE DELETE ON fleet_directive_versions
                BEGIN
                    SELECT RAISE(ABORT, 'fleet directive versions are immutable');
                END;

                CREATE TABLE IF NOT EXISTS fleet_directive_bindings (
                    id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    binding_key TEXT NOT NULL,
                    binding_value TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT,
                    UNIQUE(target_type, target_id, binding_key, version)
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_directive_bindings_target
                    ON fleet_directive_bindings (target_type, target_id, active, binding_key);

                CREATE TABLE IF NOT EXISTS fleet_directive_checks (
                    id TEXT PRIMARY KEY,
                    directive_id TEXT NOT NULL REFERENCES fleet_directives(id) ON DELETE CASCADE,
                    directive_version INTEGER NOT NULL,
                    directive_digest TEXT NOT NULL,
                    context_digest TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report TEXT NOT NULL,
                    checked_by TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_directive_checks_directive
                    ON fleet_directive_checks (directive_id, directive_version, checked_at);

                CREATE TABLE IF NOT EXISTS fleet_directive_approvals (
                    id TEXT PRIMARY KEY,
                    directive_id TEXT NOT NULL REFERENCES fleet_directives(id) ON DELETE CASCADE,
                    directive_version INTEGER NOT NULL,
                    directive_digest TEXT NOT NULL,
                    check_id TEXT NOT NULL REFERENCES fleet_directive_checks(id),
                    context_digest TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    UNIQUE(directive_id, directive_version, directive_digest)
                );

                CREATE TABLE IF NOT EXISTS fleet_directive_activations (
                    id TEXT PRIMARY KEY,
                    directive_id TEXT NOT NULL REFERENCES fleet_directives(id) ON DELETE CASCADE,
                    directive_version INTEGER NOT NULL,
                    directive_digest TEXT NOT NULL,
                    check_id TEXT NOT NULL REFERENCES fleet_directive_checks(id),
                    approval_id TEXT NOT NULL REFERENCES fleet_directive_approvals(id),
                    epoch INTEGER NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    cohort TEXT NOT NULL,
                    expected_acks INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    deactivated_at TEXT,
                    deactivated_by TEXT,
                    deactivation_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_directive_activations_state_epoch
                    ON fleet_directive_activations (state, epoch);

                CREATE TABLE IF NOT EXISTS fleet_directive_acks (
                    id TEXT PRIMARY KEY,
                    activation_id TEXT NOT NULL REFERENCES fleet_directive_activations(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL REFERENCES agents(id),
                    directive_digest TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL,
                    UNIQUE(activation_id, agent_id)
                );

                CREATE TABLE IF NOT EXISTS fleet_directive_waivers (
                    id TEXT PRIMARY KEY,
                    directive_id TEXT NOT NULL REFERENCES fleet_directives(id) ON DELETE CASCADE,
                    directive_version INTEGER NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    revoked_by TEXT,
                    revoked_at TEXT,
                    revoke_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_directive_waivers_lookup
                    ON fleet_directive_waivers (directive_id, directive_version, target_type, target_id);

                CREATE TABLE IF NOT EXISTS fleet_directive_macro_instances (
                    id TEXT PRIMARY KEY,
                    activation_id TEXT NOT NULL REFERENCES fleet_directive_activations(id) ON DELETE CASCADE,
                    repository_id TEXT NOT NULL,
                    work_package_id TEXT,
                    state TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(activation_id, repository_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_directive_macro_instances_state
                    ON fleet_directive_macro_instances (state, updated_at);

                CREATE TABLE IF NOT EXISTS agent_lifecycle_events (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_lifecycle_events_agent_created
                    ON agent_lifecycle_events (agent_id, created_at);

                -- Per-agent operational events (mood transitions, nap
                -- lifecycle, future agent-level audit). Flows through the
                -- unified events view.
                CREATE TABLE IF NOT EXISTS agent_events (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_events_agent_created
                    ON agent_events (agent_id, created_at);

                -- Append-only mood transitions. The current mood is the most
                -- recent row per agent where cleared_at IS NULL and
                -- (expires_at IS NULL OR expires_at > now). Agents pick their
                -- own mood; mac records.
                CREATE TABLE IF NOT EXISTS mood_overlays (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    mode TEXT NOT NULL,
                    reason TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    set_by TEXT NOT NULL,
                    set_at TEXT NOT NULL,
                    expires_at TEXT,
                    cleared_at TEXT,
                    cleared_by TEXT,
                    cleared_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_mood_overlays_agent_set_at
                    ON mood_overlays (agent_id, set_at);

                -- Allowlisted runtime-settable agent configuration flags
                -- (see mac/config_flags.py). channel '' = agent-global;
                -- otherwise a gateway channel key like 'slack:C123'. The
                -- effective value resolves channel row -> global row ->
                -- registry default. Audit trail lives in agent_events.
                CREATE TABLE IF NOT EXISTS agent_config_flags (
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    flag TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT '',
                    value TEXT NOT NULL,
                    set_by TEXT,
                    reason TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (agent_id, flag, channel)
                );

                -- One consolidated deploy-config document per agent: the
                -- non-secret "geek knobs" its gateway actually launched
                -- with (image tag, sandbox, home channel, model defaults,
                -- plugin tuning), self-reported at gateway startup so the
                -- effective-config view has a single place to look instead
                -- of chasing launcher scripts, runtime.env, and plugin
                -- constants across hosts. Secrets are rejected on write
                -- (see agent_state_service). Audit trail: agent_events.
                CREATE TABLE IF NOT EXISTS agent_deploy_configs (
                    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                    document TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    reported_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS nap_schedules (
                    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                    offset_minutes INTEGER NOT NULL,
                    window_minutes INTEGER NOT NULL DEFAULT 15,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_completed_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS nap_runs (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    summary_evidence_id TEXT,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_nap_runs_agent_started
                    ON nap_runs (agent_id, started_at);

                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    reviewer_agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    evidence_id TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_reviews_task_status
                    ON reviews (task_id, status);

                CREATE TABLE IF NOT EXISTS publications (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_id TEXT,
                    content_hash TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS secrets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    scopes TEXT NOT NULL,
                    ciphertext TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    rotated_at TEXT,
                    enabled INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS secret_access_audit (
                    id TEXT PRIMARY KEY,
                    secret_id TEXT NOT NULL REFERENCES secrets(id) ON DELETE CASCADE,
                    accessor_agent_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    result TEXT NOT NULL,
                    expires_at TEXT,
                    revealed_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_secret_audit_secret_created
                    ON secret_access_audit (secret_id, created_at);

                -- Gateway-side provenance: who is talking to which Hermes
                -- instance, in which platform thread, about which task.
                -- Content stays in Hermes; mac only records the pointer.
                CREATE TABLE IF NOT EXISTS conversation_threads (
                    id TEXT PRIMARY KEY,
                    platform_binding_id TEXT NOT NULL REFERENCES platform_bindings(id) ON DELETE CASCADE,
                    external_thread_id TEXT NOT NULL,
                    latest_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    summary TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(platform_binding_id, external_thread_id)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_threads_binding
                    ON conversation_threads (platform_binding_id, last_seen_at);

                -- Vector-memory-side provenance: a Hermes memory record may be
                -- mirrored into a vector store (Qdrant, pgvector, etc.). mac
                -- never stores embeddings; it only audits "this memory was
                -- indexed at this point id in this collection."
                CREATE TABLE IF NOT EXISTS vector_refs (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
                    vector_db TEXT NOT NULL,
                    collection TEXT NOT NULL,
                    point_id TEXT NOT NULL,
                    embedding_model TEXT,
                    metadata TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(vector_db, collection, point_id)
                );
                CREATE INDEX IF NOT EXISTS idx_vector_refs_memory
                    ON vector_refs (memory_id);

                CREATE TABLE IF NOT EXISTS environments (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    tenant_id TEXT REFERENCES tenants(id) ON DELETE SET NULL,
                    channel TEXT NOT NULL DEFAULT 'fleet',
                    promotes_from TEXT REFERENCES environments(id) ON DELETE SET NULL,
                    metadata TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, name)
                );

                CREATE TABLE IF NOT EXISTS environment_events (
                    id TEXT PRIMARY KEY,
                    environment_id TEXT NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_environment_events_env
                    ON environment_events (environment_id, created_at);

                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    environment_id TEXT NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
                    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
                    status TEXT NOT NULL,
                    deployed_by TEXT NOT NULL,
                    deployed_at TEXT NOT NULL,
                    retired_at TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_deployments_env_active
                    ON deployments (environment_id, retired_at);

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    digest TEXT NOT NULL UNIQUE,
                    uri TEXT NOT NULL,
                    sbom_uri TEXT,
                    signers TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_kind
                    ON artifacts (kind);

                CREATE TABLE IF NOT EXISTS runtime_environments (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    manifest TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_environment_deltas (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    project TEXT,
                    base_runtime_id TEXT REFERENCES runtime_environments(id) ON DELETE SET NULL,
                    base_runtime_digest TEXT,
                    package_manager TEXT NOT NULL,
                    commands TEXT NOT NULL,
                    added_dependencies TEXT NOT NULL,
                    lockfile_path TEXT,
                    lockfile_digest TEXT,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    validation TEXT NOT NULL DEFAULT '{}',
                    evidence_id TEXT REFERENCES evidence(id) ON DELETE SET NULL,
                    promoted_runtime_environment_id TEXT REFERENCES runtime_environments(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    validated_at TEXT,
                    promoted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_deltas_status
                    ON runtime_environment_deltas (status, created_at);
                CREATE INDEX IF NOT EXISTS idx_runtime_deltas_task
                    ON runtime_environment_deltas (task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_runtime_deltas_project
                    ON runtime_environment_deltas (project, created_at);

                CREATE TABLE IF NOT EXISTS runtime_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL,
                    environment_id TEXT NOT NULL REFERENCES runtime_environments(id),
                    status TEXT NOT NULL,
                    evidence_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_projects_status_name
                    ON projects (status, name);

                CREATE TABLE IF NOT EXISTS project_events (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_project_events_project_created
                    ON project_events (project_id, created_at);

                CREATE TABLE IF NOT EXISTS project_items (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source, external_id)
                );

                CREATE TABLE IF NOT EXISTS project_repositories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    path TEXT NOT NULL,
                    source TEXT NOT NULL UNIQUE,
                    project TEXT NOT NULL,
                    required_capabilities TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    poll_interval_seconds INTEGER NOT NULL DEFAULT 60,
                    last_polled_at TEXT,
                    last_imported_at TEXT,
                    last_error TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_project_repositories_enabled
                    ON project_repositories (enabled, last_polled_at);

                CREATE TABLE IF NOT EXISTS integration_observations (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    authority TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fingerprint TEXT,
                    cursor TEXT,
                    detail TEXT NOT NULL DEFAULT '{}',
                    observed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_integration_observations_source
                    ON integration_observations (source_kind, source_id, observed_at);

                CREATE TABLE IF NOT EXISTS integration_findings (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    finding_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    fingerprint TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution TEXT,
                    UNIQUE(source_kind, source_id, finding_type, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_integration_findings_status
                    ON integration_findings (status, severity, last_seen_at);

                CREATE TABLE IF NOT EXISTS memory_records (
                    id TEXT PRIMARY KEY,
                    task_id TEXT,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT,
                    record_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    evidence_id TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_task_created
                    ON memory_records (task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_memory_subject
                    ON memory_records (subject_type, subject_id);

                CREATE TABLE IF NOT EXISTS rollouts (
                    id TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_percent INTEGER NOT NULL,
                    tenant_id TEXT,
                    channel TEXT NOT NULL DEFAULT 'fleet',
                    runtime_environment_id TEXT,
                    artifact_uri TEXT,
                    artifact_hash TEXT,
                    health_policy TEXT NOT NULL DEFAULT '{}',
                    deploy_environment_id TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rollout_events (
                    id TEXT PRIMARY KEY,
                    rollout_id TEXT NOT NULL REFERENCES rollouts(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS eval_sets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    scoring TEXT NOT NULL,
                    baseline_score REAL,
                    regression_threshold REAL NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS eval_runs (
                    id TEXT PRIMARY KEY,
                    eval_set_id TEXT NOT NULL REFERENCES eval_sets(id) ON DELETE CASCADE,
                    target_kind TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    score REAL NOT NULL,
                    baseline_score REAL,
                    delta REAL,
                    threshold REAL NOT NULL,
                    passed INTEGER NOT NULL,
                    detail TEXT NOT NULL,
                    evidence_id TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_eval_runs_set_target
                    ON eval_runs (eval_set_id, target_kind, target_id, created_at);

                CREATE TABLE IF NOT EXISTS eval_set_events (
                    id TEXT PRIMARY KEY,
                    eval_set_id TEXT NOT NULL REFERENCES eval_sets(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_eval_set_events_set
                    ON eval_set_events (eval_set_id, created_at);

                CREATE TABLE IF NOT EXISTS scientific_policies (
                    id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    project TEXT NOT NULL,
                    name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    parameters TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project, name, version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_scientific_policies_one_active
                    ON scientific_policies(project) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_scientific_policies_project_status
                    ON scientific_policies(project, status, updated_at);

                CREATE TABLE IF NOT EXISTS scientific_experiments (
                    id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    project TEXT NOT NULL,
                    name TEXT NOT NULL,
                    hypothesis TEXT NOT NULL,
                    state TEXT NOT NULL,
                    running_slot TEXT UNIQUE,
                    control_policy_id TEXT NOT NULL,
                    treatment_policy_id TEXT NOT NULL,
                    primary_metric TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    min_effect REAL NOT NULL DEFAULT 0,
                    quality_margin REAL NOT NULL DEFAULT 0.05,
                    min_samples_per_arm INTEGER NOT NULL,
                    max_samples_per_arm INTEGER NOT NULL,
                    exploration_fraction REAL NOT NULL,
                    outcome_horizon_seconds REAL NOT NULL,
                    guardrails TEXT NOT NULL DEFAULT '{}',
                    auto_promote INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(control_policy_id) REFERENCES scientific_policies(id),
                    FOREIGN KEY(treatment_policy_id) REFERENCES scientific_policies(id)
                );
                CREATE INDEX IF NOT EXISTS idx_scientific_experiments_project_state
                    ON scientific_experiments(project, state, created_at);

                CREATE TABLE IF NOT EXISTS scientific_assignments (
                    experiment_id TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    arm TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    propensity REAL NOT NULL,
                    stratum TEXT NOT NULL DEFAULT '',
                    assignment TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY(experiment_id, task_id),
                    FOREIGN KEY(experiment_id) REFERENCES scientific_experiments(id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(policy_id) REFERENCES scientific_policies(id)
                );
                CREATE INDEX IF NOT EXISTS idx_scientific_assignments_experiment_arm
                    ON scientific_assignments(experiment_id, phase, arm, assigned_at);

                CREATE TABLE IF NOT EXISTS scientific_observations (
                    experiment_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    arm TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    terminal INTEGER NOT NULL DEFAULT 0,
                    quality_validated INTEGER NOT NULL DEFAULT 0,
                    metrics TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(experiment_id, task_id),
                    FOREIGN KEY(experiment_id) REFERENCES scientific_experiments(id) ON DELETE CASCADE,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS scientific_decisions (
                    id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES scientific_experiments(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_scientific_decisions_experiment
                    ON scientific_decisions(experiment_id, created_at);

                CREATE TABLE IF NOT EXISTS scientific_optimizer_events (
                    id TEXT PRIMARY KEY,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scientific_optimizer_events_subject
                    ON scientific_optimizer_events(subject_type, subject_id, created_at);

                CREATE TABLE IF NOT EXISTS scientific_optimizer_locks (
                    name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_roles (
                    id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL,
                    name TEXT NOT NULL,
                    display_name TEXT,
                    description TEXT NOT NULL,
                    system_prompt TEXT NOT NULL,
                    level TEXT NOT NULL,
                    reports_to TEXT REFERENCES agent_roles(id) ON DELETE SET NULL,
                    specialties TEXT NOT NULL DEFAULT '[]',
                    default_capabilities TEXT NOT NULL DEFAULT '[]',
                    required_capabilities TEXT NOT NULL DEFAULT '[]',
                    hardware_requirements TEXT NOT NULL DEFAULT '{}',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(slug, tenant_id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_roles_slug_tenant
                    ON agent_roles (slug, tenant_id);
                CREATE INDEX IF NOT EXISTS idx_agent_roles_reports_to
                    ON agent_roles (reports_to);

                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    workflow_type TEXT NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    definition TEXT NOT NULL,
                    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(slug, tenant_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_workflows_type_enabled
                    ON workflows (workflow_type, enabled);

                CREATE TABLE IF NOT EXISTS workflow_drafts (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    proposed_steps TEXT NOT NULL DEFAULT '[]',
                    questions TEXT NOT NULL DEFAULT '[]',
                    answers TEXT NOT NULL DEFAULT '{}',
                    edit_history TEXT NOT NULL DEFAULT '[]',
                    compiled_workflow_id TEXT REFERENCES workflows(id) ON DELETE SET NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    approved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_drafts_status
                    ON workflow_drafts (status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_workflow_drafts_tenant
                    ON workflow_drafts (tenant_id, updated_at);

                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
                    workflow_version INTEGER NOT NULL,
                    definition_snapshot TEXT NOT NULL,
                    state TEXT NOT NULL,
                    current_node_key TEXT,
                    current_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    input TEXT NOT NULL DEFAULT '{}',
                    context TEXT NOT NULL DEFAULT '{}',
                    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
                    started_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    next_action_at TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_state
                    ON workflow_runs (state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_current_task
                    ON workflow_runs (current_task_id);
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow
                    ON workflow_runs (workflow_id, created_at);

                CREATE TABLE IF NOT EXISTS workflow_run_history (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    from_node_key TEXT,
                    to_node_key TEXT,
                    condition TEXT NOT NULL,
                    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    actor TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_run_history_run
                    ON workflow_run_history (run_id, seq);

                -- Work packages are the durable unit of coordinated parallel
                -- work.  A stable package points at immutable plan versions
                -- and base-pinned execution epochs; concrete tasks remain in
                -- the canonical tasks table and are linked to one plan node.
                CREATE TABLE IF NOT EXISTS work_packages (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT REFERENCES tenants(id) ON DELETE RESTRICT,
                    project TEXT,
                    repository_id TEXT REFERENCES project_repositories(id) ON DELETE RESTRICT,
                    root_task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
                    goal TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN (
                        'draft', 'admitted', 'active', 'paused', 'replanning',
                        'completed', 'failed', 'cancelled'
                    )),
                    current_plan_version INTEGER NOT NULL DEFAULT 0
                        CHECK (current_plan_version >= 0),
                    current_epoch INTEGER NOT NULL DEFAULT 0
                        CHECK (current_epoch >= 0),
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE (id, repository_id),
                    CHECK (
                        state IN ('draft', 'cancelled') OR
                        (current_plan_version >= 1 AND current_epoch >= 1)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_work_packages_state
                    ON work_packages (state, updated_at, id);
                CREATE INDEX IF NOT EXISTS idx_work_packages_project
                    ON work_packages (project, state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_work_packages_repository
                    ON work_packages (repository_id, state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_work_packages_root_task
                    ON work_packages (root_task_id);

                CREATE TRIGGER IF NOT EXISTS trg_work_packages_initial_state
                BEFORE INSERT ON work_packages
                WHEN NEW.state != 'draft'
                BEGIN
                    SELECT RAISE(ABORT, 'work packages must start draft');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_packages_state_transition
                BEFORE UPDATE OF state ON work_packages
                WHEN NEW.state != OLD.state AND NOT (
                    (OLD.state = 'draft' AND NEW.state IN ('admitted', 'cancelled')) OR
                    (OLD.state = 'admitted' AND NEW.state IN (
                        'active', 'paused', 'failed', 'cancelled'
                    )) OR
                    (OLD.state = 'active' AND NEW.state IN (
                        'paused', 'replanning', 'completed', 'failed', 'cancelled'
                    )) OR
                    (OLD.state = 'paused' AND NEW.state IN (
                        'active', 'replanning', 'failed', 'cancelled'
                    )) OR
                    (OLD.state = 'replanning' AND NEW.state IN (
                        'active', 'paused', 'failed', 'cancelled'
                    ))
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid work package state transition');
                END;

                CREATE TABLE IF NOT EXISTS work_package_plan_versions (
                    package_id TEXT NOT NULL REFERENCES work_packages(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    parent_version INTEGER,
                    definition TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (package_id, version),
                    UNIQUE (package_id, plan_digest),
                    CHECK (parent_version IS NULL OR (
                        parent_version >= 1 AND parent_version < version
                    )),
                    FOREIGN KEY (package_id, parent_version)
                        REFERENCES work_package_plan_versions(package_id, version)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_plan_digest
                    ON work_package_plan_versions (plan_digest);

                CREATE TABLE IF NOT EXISTS work_package_epochs (
                    package_id TEXT NOT NULL REFERENCES work_packages(id) ON DELETE CASCADE,
                    epoch INTEGER NOT NULL CHECK (epoch >= 1),
                    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
                    planning_base_ref TEXT NOT NULL,
                    planning_base_sha TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN (
                        'staged', 'active', 'superseded', 'completed', 'cancelled'
                    )),
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT,
                    PRIMARY KEY (package_id, epoch),
                    UNIQUE (package_id, epoch, plan_version),
                    FOREIGN KEY (package_id, plan_version)
                        REFERENCES work_package_plan_versions(package_id, version)
                        ON DELETE RESTRICT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_work_package_active_epoch
                    ON work_package_epochs (package_id) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_work_package_epochs_plan
                    ON work_package_epochs (package_id, plan_version, epoch);

                CREATE TRIGGER IF NOT EXISTS trg_work_packages_current_epoch_insert
                BEFORE INSERT ON work_packages
                WHEN NEW.state NOT IN ('draft', 'cancelled') AND NOT EXISTS (
                    SELECT 1 FROM work_package_epochs AS epoch
                    WHERE epoch.package_id = NEW.id
                      AND epoch.epoch = NEW.current_epoch
                      AND epoch.plan_version = NEW.current_plan_version
                      AND (
                          NEW.state NOT IN ('admitted', 'active', 'paused') OR
                          epoch.status = 'active'
                      )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'work package current epoch/version is incoherent');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_packages_current_epoch_update
                BEFORE UPDATE OF state, current_plan_version, current_epoch ON work_packages
                WHEN NEW.state NOT IN ('draft', 'cancelled') AND NOT EXISTS (
                    SELECT 1 FROM work_package_epochs AS epoch
                    WHERE epoch.package_id = NEW.id
                      AND epoch.epoch = NEW.current_epoch
                      AND epoch.plan_version = NEW.current_plan_version
                      AND (
                          NEW.state NOT IN ('admitted', 'active', 'paused') OR
                          epoch.status = 'active'
                      )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'work package current epoch/version is incoherent');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_current_epoch_status
                BEFORE UPDATE OF status ON work_package_epochs
                WHEN NEW.status != 'active' AND EXISTS (
                    SELECT 1 FROM work_packages AS package
                    WHERE package.id = NEW.package_id
                      AND package.current_epoch = NEW.epoch
                      AND package.current_plan_version = NEW.plan_version
                      AND package.state IN ('admitted', 'active', 'paused')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'cannot deactivate a runnable package current epoch');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_current_epoch_delete
                BEFORE DELETE ON work_package_epochs
                WHEN EXISTS (
                    SELECT 1 FROM work_packages AS package
                    WHERE package.id = OLD.package_id
                      AND package.current_epoch = OLD.epoch
                      AND package.current_plan_version = OLD.plan_version
                      AND package.state IN ('admitted', 'active', 'paused')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'cannot delete a runnable package current epoch');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_plan_versions_immutable
                BEFORE UPDATE ON work_package_plan_versions
                BEGIN
                    SELECT RAISE(ABORT, 'work package plan versions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_plan_versions_no_delete
                BEFORE DELETE ON work_package_plan_versions
                BEGIN
                    SELECT RAISE(ABORT, 'work package plan versions are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_epochs_identity_immutable
                BEFORE UPDATE OF
                    package_id, epoch, plan_version, planning_base_ref,
                    planning_base_sha, reason, created_by, created_at
                ON work_package_epochs
                BEGIN
                    SELECT RAISE(ABORT, 'work package epoch identity is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_epochs_state_transition
                BEFORE UPDATE OF status ON work_package_epochs
                WHEN NEW.status != OLD.status AND NOT (
                    (OLD.status = 'staged' AND NEW.status IN ('active', 'cancelled')) OR
                    (OLD.status = 'active' AND NEW.status IN (
                        'superseded', 'completed', 'cancelled'
                    ))
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid work package epoch state transition');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_epochs_no_delete
                BEFORE DELETE ON work_package_epochs
                BEGIN
                    SELECT RAISE(ABORT, 'work package epochs are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_task_links (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE RESTRICT,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    node_key TEXT NOT NULL,
                    node_generation INTEGER NOT NULL CHECK (node_generation >= 1),
                    declared_effects_digest TEXT NOT NULL,
                    contract_digest TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    node_state TEXT NOT NULL CHECK (node_state IN (
                        'planned', 'ready', 'executing', 'candidate_submitted',
                        'candidate_accepted', 'integrated', 'certified',
                        'rejected', 'superseded', 'cancelled'
                    )),
                    created_at TEXT NOT NULL,
                    UNIQUE (package_id, epoch, node_key),
                    UNIQUE (package_id, node_key, node_generation),
                    UNIQUE (package_id, plan_version, epoch, node_key, task_id),
                    UNIQUE (
                        package_id, plan_version, epoch, node_key,
                        node_generation, task_id
                    ),
                    UNIQUE (
                        package_id, plan_version, epoch, node_key,
                        task_id, declared_effects_digest
                    ),
                    FOREIGN KEY (package_id, epoch, plan_version)
                        REFERENCES work_package_epochs(package_id, epoch, plan_version)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_task_links_package
                    ON work_package_task_links (package_id, epoch, node_key);
                CREATE INDEX IF NOT EXISTS idx_work_package_task_links_state
                    ON work_package_task_links (package_id, epoch, node_state, node_key);

                CREATE TRIGGER IF NOT EXISTS trg_work_package_task_links_identity_immutable
                BEFORE UPDATE OF
                    task_id, package_id, plan_version, epoch, node_key,
                    node_generation, declared_effects_digest,
                    contract_digest, input_digest, created_at
                ON work_package_task_links
                BEGIN
                    SELECT RAISE(ABORT, 'work package task-link identity is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_task_links_initial_state
                BEFORE INSERT ON work_package_task_links
                WHEN NEW.node_state != 'planned'
                BEGIN
                    SELECT RAISE(ABORT, 'work package task links must start planned');
                END;

                -- Close the inverse mixed-version ordering: a legacy writer
                -- cannot first create/claim an ordinary task and only then
                -- attach it to a package, bypassing the task UPDATE guard.
                CREATE TRIGGER IF NOT EXISTS trg_work_package_task_link_executable_insert
                BEFORE INSERT ON work_package_task_links
                WHEN EXISTS (
                    SELECT 1 FROM tasks AS task
                    WHERE task.id = NEW.task_id
                      AND task.state IN ('claimed', 'running')
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'executable task cannot be linked without package claim authority'
                    );
                END;

                DROP TRIGGER IF EXISTS trg_work_package_task_links_state_transition;
                CREATE TRIGGER trg_work_package_task_links_state_transition
                BEFORE UPDATE OF node_state ON work_package_task_links
                WHEN NEW.node_state != OLD.node_state AND NOT (
                    (OLD.node_state = 'planned' AND NEW.node_state IN (
                        'ready', 'superseded', 'cancelled'
                    )) OR
                    (OLD.node_state = 'ready' AND NEW.node_state IN (
                        'executing', 'superseded', 'cancelled'
                    )) OR
                    (OLD.node_state = 'executing' AND NEW.node_state IN (
                        'ready', 'candidate_submitted', 'rejected', 'cancelled'
                    )) OR
                    (OLD.node_state = 'candidate_submitted' AND NEW.node_state IN (
                        'candidate_accepted', 'rejected', 'superseded'
                    )) OR
                    (OLD.node_state = 'candidate_accepted' AND NEW.node_state IN (
                        'integrated', 'rejected', 'superseded'
                    )) OR
                    (OLD.node_state = 'integrated' AND NEW.node_state IN (
                        'certified', 'rejected', 'superseded'
                    )) OR
                    (OLD.node_state = 'rejected' AND NEW.node_state = 'executing')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid work package node state transition');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_task_links_no_delete
                BEFORE DELETE ON work_package_task_links
                BEGIN
                    SELECT RAISE(ABORT, 'work package task links are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_node_lineage (
                    id TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL,
                    from_plan_version INTEGER NOT NULL,
                    from_epoch INTEGER NOT NULL,
                    from_node_key TEXT NOT NULL,
                    from_task_id TEXT NOT NULL,
                    to_plan_version INTEGER NOT NULL,
                    to_epoch INTEGER NOT NULL,
                    to_node_key TEXT NOT NULL,
                    to_task_id TEXT NOT NULL,
                    relation TEXT NOT NULL CHECK (relation IN (
                        'carried_forward', 'replaced', 'split', 'merged', 'invalidated'
                    )),
                    contract_digest TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    source_evidence_id TEXT,
                    decision TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (
                        package_id, from_plan_version, from_epoch, from_node_key,
                        to_plan_version, to_epoch, to_node_key
                    ),
                    FOREIGN KEY (
                        package_id, from_plan_version, from_epoch,
                        from_node_key, from_task_id
                    ) REFERENCES work_package_task_links (
                        package_id, plan_version, epoch, node_key, task_id
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        package_id, to_plan_version, to_epoch,
                        to_node_key, to_task_id
                    ) REFERENCES work_package_task_links (
                        package_id, plan_version, epoch, node_key, task_id
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (source_evidence_id, from_task_id)
                        REFERENCES evidence(id, task_id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_node_lineage_from
                    ON work_package_node_lineage (
                        package_id, from_plan_version, from_epoch, from_node_key
                    );
                CREATE INDEX IF NOT EXISTS idx_work_package_node_lineage_to
                    ON work_package_node_lineage (
                        package_id, to_plan_version, to_epoch, to_node_key
                    );
                CREATE TRIGGER IF NOT EXISTS trg_work_package_node_lineage_immutable
                BEFORE UPDATE ON work_package_node_lineage
                BEGIN
                    SELECT RAISE(ABORT, 'work package node lineage is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_node_lineage_no_delete
                BEFORE DELETE ON work_package_node_lineage
                BEGIN
                    SELECT RAISE(ABORT, 'work package node lineage is append-only');
                END;

                -- Composite identities let the audit rows prove that the
                -- selected task and agent are exactly those bound to the lease.
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_leases_assignment_identity
                    ON leases (id, task_id, agent_id);
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_evidence_task_identity
                    ON evidence (id, task_id);
                CREATE TABLE IF NOT EXISTS evidence_attempt_links (
                    evidence_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                    attempt_ref TEXT NOT NULL,
                    attempt_base_sha TEXT NOT NULL,
                    attempt_head_sha TEXT,
                    artifact_digest TEXT,
                    declared_effects_digest TEXT,
                    observed_effects_digest TEXT,
                    protected_ref INTEGER NOT NULL DEFAULT 0
                        CHECK (protected_ref IN (0, 1)),
                    controller_verified INTEGER NOT NULL DEFAULT 0
                        CHECK (controller_verified IN (0, 1)),
                    controller_verifier TEXT,
                    controller_verified_at TEXT,
                    created_at TEXT NOT NULL,
                    CHECK (protected_ref = 0 OR attempt_ref LIKE 'refs/mac/%'),
                    CHECK (
                        (controller_verified = 0 AND controller_verifier IS NULL
                         AND controller_verified_at IS NULL) OR
                        (controller_verified = 1 AND protected_ref = 1
                         AND attempt_head_sha IS NOT NULL
                         AND artifact_digest IS NOT NULL
                         AND controller_verifier IS NOT NULL
                         AND controller_verified_at IS NOT NULL)
                    ),
                    UNIQUE (evidence_id, task_id, lease_id, attempt_number),
                    FOREIGN KEY (evidence_id, task_id)
                        REFERENCES evidence(id, task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (lease_id, task_id, agent_id)
                        REFERENCES leases(id, task_id, agent_id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_attempt_links_lease
                    ON evidence_attempt_links (lease_id, attempt_number, evidence_id);
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_evidence_attempt_verification_identity
                    ON evidence_attempt_links (
                        evidence_id, task_id, lease_id, agent_id, attempt_number,
                        attempt_ref, attempt_base_sha, declared_effects_digest
                    );
                CREATE TRIGGER IF NOT EXISTS trg_evidence_attempt_links_immutable
                BEFORE UPDATE ON evidence_attempt_links
                BEGIN
                    SELECT RAISE(ABORT, 'evidence attempt attribution is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_evidence_attempt_links_no_delete
                BEFORE DELETE ON evidence_attempt_links
                BEGIN
                    SELECT RAISE(ABORT, 'evidence attempt attribution is append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_assignment_audit (
                    lease_id TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    node_key TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                    attempt_ref TEXT NOT NULL,
                    attempt_base_ref TEXT NOT NULL,
                    attempt_base_sha TEXT NOT NULL,
                    declared_effects_digest TEXT NOT NULL,
                    allocator TEXT NOT NULL,
                    allocator_version TEXT NOT NULL,
                    score REAL,
                    rationale TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE (
                        lease_id, package_id, plan_version, epoch, node_key,
                        task_id
                    ),
                    UNIQUE (
                        lease_id, package_id, plan_version, epoch, node_key,
                        task_id, attempt_number
                    ),
                    FOREIGN KEY (
                        package_id, plan_version, epoch, node_key,
                        task_id, declared_effects_digest
                    )
                        REFERENCES work_package_task_links(
                            package_id, plan_version, epoch, node_key,
                            task_id, declared_effects_digest
                        ) ON DELETE RESTRICT,
                    FOREIGN KEY (lease_id, task_id, agent_id)
                        REFERENCES leases(id, task_id, agent_id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_assignment_package
                    ON work_package_assignment_audit (package_id, epoch, created_at);
                CREATE INDEX IF NOT EXISTS idx_work_package_assignment_agent
                    ON work_package_assignment_audit (agent_id, created_at);

                CREATE TRIGGER IF NOT EXISTS trg_work_package_assignment_immutable
                BEFORE UPDATE ON work_package_assignment_audit
                BEGIN
                    SELECT RAISE(ABORT, 'work package assignment audit is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_assignment_no_delete
                BEFORE DELETE ON work_package_assignment_audit
                BEGIN
                    SELECT RAISE(ABORT, 'work package assignment audit is append-only');
                END;

                -- Mixed-version safety boundary: an older hub may know how to
                -- claim an ordinary task but not the package allocator.  A
                -- linked task cannot enter or remain in an executable worker
                -- state unless this exact lease generation already has the
                -- immutable assignment audit produced by the package gate.
                CREATE TRIGGER IF NOT EXISTS trg_work_package_task_claim_authority
                BEFORE UPDATE OF state, owner_agent_id, lease_id, attempt_count ON tasks
                WHEN NEW.state IN ('claimed', 'running')
                  AND EXISTS (
                      SELECT 1 FROM work_package_task_links AS linked
                      WHERE linked.task_id = NEW.id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM work_package_assignment_audit AS assignment
                      JOIN work_package_task_links AS linked
                        ON linked.package_id = assignment.package_id
                       AND linked.plan_version = assignment.plan_version
                       AND linked.epoch = assignment.epoch
                       AND linked.node_key = assignment.node_key
                       AND linked.task_id = assignment.task_id
                       AND linked.declared_effects_digest = assignment.declared_effects_digest
                      JOIN leases AS lease
                        ON lease.id = assignment.lease_id
                       AND lease.task_id = assignment.task_id
                       AND lease.agent_id = assignment.agent_id
                      WHERE assignment.task_id = NEW.id
                        AND assignment.lease_id = NEW.lease_id
                        AND assignment.agent_id = NEW.owner_agent_id
                        AND assignment.attempt_number = NEW.attempt_count
                        AND lease.status = 'active'
                        AND linked.node_state = 'executing'
                  )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'work package task claim lacks exact assignment authority'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS trg_evidence_attempt_package_identity
                BEFORE INSERT ON evidence_attempt_links
                WHEN EXISTS (
                    SELECT 1 FROM work_package_assignment_audit AS assignment
                    WHERE assignment.lease_id = NEW.lease_id
                ) AND NOT EXISTS (
                    SELECT 1 FROM work_package_assignment_audit AS assignment
                    WHERE assignment.lease_id = NEW.lease_id
                      AND assignment.task_id = NEW.task_id
                      AND assignment.agent_id = NEW.agent_id
                      AND assignment.attempt_number = NEW.attempt_number
                      AND assignment.attempt_ref = NEW.attempt_ref
                      AND assignment.attempt_base_sha = NEW.attempt_base_sha
                      AND assignment.declared_effects_digest = NEW.declared_effects_digest
                )
                BEGIN
                    SELECT RAISE(ABORT, 'evidence attempt does not match package assignment');
                END;

                -- Worker evidence is attribution only.  Controller-observed
                -- repository facts are published separately so verification
                -- can never mutate or silently upgrade a worker-authored row.
                CREATE TABLE IF NOT EXISTS evidence_attempt_verifications (
                    id TEXT PRIMARY KEY,
                    evidence_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                    repository_id TEXT NOT NULL
                        REFERENCES project_repositories(id) ON DELETE RESTRICT,
                    attempt_ref TEXT NOT NULL CHECK (
                        attempt_ref LIKE 'refs/mac/attempts/%'
                    ),
                    attempt_base_sha TEXT NOT NULL CHECK (
                        length(attempt_base_sha) IN (40, 64) AND
                        attempt_base_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    attempt_head_sha TEXT NOT NULL CHECK (
                        length(attempt_head_sha) IN (40, 64) AND
                        attempt_head_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    tree_digest TEXT NOT NULL CHECK (
                        length(tree_digest) = 71 AND
                        tree_digest LIKE 'sha256:%' AND
                        substr(tree_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    declared_effects_digest TEXT NOT NULL,
                    observed_effects_digest TEXT NOT NULL CHECK (
                        length(observed_effects_digest) = 71 AND
                        observed_effects_digest LIKE 'sha256:%' AND
                        substr(observed_effects_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    changed_paths TEXT NOT NULL DEFAULT '[]'
                        CHECK (
                            json_valid(changed_paths) AND
                            json_type(changed_paths) = 'array'
                        ),
                    changes TEXT NOT NULL DEFAULT '[]' CHECK (
                        json_valid(changes) AND json_type(changes) = 'array'
                    ),
                    verifier TEXT NOT NULL CHECK (verifier != ''),
                    verifier_version TEXT NOT NULL CHECK (verifier_version != ''),
                    verified_at TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL UNIQUE CHECK (
                        length(receipt_digest) = 71 AND
                        receipt_digest LIKE 'sha256:%' AND
                        substr(receipt_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    FOREIGN KEY (
                        evidence_id, task_id, lease_id, agent_id, attempt_number,
                        attempt_ref, attempt_base_sha, declared_effects_digest
                    ) REFERENCES evidence_attempt_links (
                        evidence_id, task_id, lease_id, agent_id, attempt_number,
                        attempt_ref, attempt_base_sha, declared_effects_digest
                    ) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_attempt_verifications_lease
                    ON evidence_attempt_verifications (
                        lease_id, attempt_number, evidence_id
                    );
                CREATE TRIGGER IF NOT EXISTS trg_evidence_attempt_verification_identity
                BEFORE INSERT ON evidence_attempt_verifications
                WHEN NOT EXISTS (
                    SELECT 1
                    FROM work_package_assignment_audit AS assignment
                    JOIN work_package_task_links AS link
                      ON link.package_id = assignment.package_id
                     AND link.plan_version = assignment.plan_version
                     AND link.epoch = assignment.epoch
                     AND link.node_key = assignment.node_key
                     AND link.task_id = assignment.task_id
                    JOIN work_packages AS package
                      ON package.id = assignment.package_id
                    WHERE assignment.lease_id = NEW.lease_id
                      AND assignment.task_id = NEW.task_id
                      AND assignment.agent_id = NEW.agent_id
                      AND assignment.attempt_number = NEW.attempt_number
                      AND assignment.attempt_ref = NEW.attempt_ref
                      AND assignment.attempt_base_sha = NEW.attempt_base_sha
                      AND assignment.declared_effects_digest =
                          NEW.declared_effects_digest
                      AND package.repository_id = NEW.repository_id
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'attempt verification does not match package assignment'
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS trg_evidence_attempt_verifications_immutable
                BEFORE UPDATE ON evidence_attempt_verifications
                BEGIN
                    SELECT RAISE(ABORT, 'attempt verification receipts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_evidence_attempt_verifications_no_delete
                BEFORE DELETE ON evidence_attempt_verifications
                BEGIN
                    SELECT RAISE(ABORT, 'attempt verification receipts are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_node_candidates (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    node_key TEXT NOT NULL,
                    node_generation INTEGER NOT NULL CHECK (node_generation >= 1),
                    assignment_lease_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                    evidence_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN (
                        'submitted', 'accepted', 'rejected', 'superseded'
                    )),
                    submitted_at TEXT NOT NULL,
                    accepted_at TEXT,
                    accepted_by TEXT,
                    rejection_reason TEXT,
                    UNIQUE (task_id, assignment_lease_id, attempt_number),
                    UNIQUE (evidence_id),
                    UNIQUE (
                        id, task_id, package_id, plan_version, epoch, node_key,
                        node_generation, assignment_lease_id, attempt_number,
                        evidence_id, status
                    ),
                    FOREIGN KEY (
                        package_id, plan_version, epoch, node_key,
                        node_generation, task_id
                    ) REFERENCES work_package_task_links (
                        package_id, plan_version, epoch, node_key,
                        node_generation, task_id
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        assignment_lease_id, package_id, plan_version, epoch,
                        node_key, task_id, attempt_number
                    ) REFERENCES work_package_assignment_audit (
                        lease_id, package_id, plan_version, epoch,
                        node_key, task_id, attempt_number
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        evidence_id, task_id, assignment_lease_id, attempt_number
                    ) REFERENCES evidence_attempt_links (
                        evidence_id, task_id, lease_id, attempt_number
                    ) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_node_candidates_package
                    ON work_package_node_candidates (package_id, epoch, status, node_key);
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_work_package_accepted_candidate
                    ON work_package_node_candidates (
                        package_id, epoch, node_key, node_generation
                    ) WHERE status = 'accepted';

                CREATE TRIGGER IF NOT EXISTS trg_work_package_node_candidate_identity
                BEFORE UPDATE OF
                    id, task_id, package_id, plan_version, epoch, node_key,
                    node_generation, assignment_lease_id, attempt_number,
                    evidence_id, submitted_at
                ON work_package_node_candidates
                BEGIN
                    SELECT RAISE(ABORT, 'work package candidate identity is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_node_candidate_state
                BEFORE UPDATE OF status ON work_package_node_candidates
                WHEN NEW.status != OLD.status AND NOT (
                    OLD.status = 'submitted' AND
                    NEW.status IN ('accepted', 'rejected', 'superseded')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid work package candidate state transition');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_node_candidate_initial_metadata
                BEFORE INSERT ON work_package_node_candidates
                WHEN NEW.status != 'submitted' OR NEW.accepted_at IS NOT NULL
                 OR NEW.accepted_by IS NOT NULL OR NEW.rejection_reason IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'work package candidates must start submitted');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_node_candidate_terminal_metadata
                BEFORE UPDATE ON work_package_node_candidates
                WHEN (
                    NEW.status = 'accepted' AND (
                        NEW.accepted_at IS NULL OR NEW.accepted_by IS NULL OR
                        NEW.rejection_reason IS NOT NULL
                    )
                ) OR (
                    NEW.status = 'rejected' AND (
                        NEW.accepted_at IS NOT NULL OR NEW.accepted_by IS NOT NULL OR
                        NEW.rejection_reason IS NULL OR NEW.rejection_reason = ''
                    )
                ) OR (
                    NEW.status IN ('submitted', 'superseded') AND (
                        NEW.accepted_at IS NOT NULL OR NEW.accepted_by IS NOT NULL OR
                        NEW.rejection_reason IS NOT NULL
                    )
                ) OR (
                    (NEW.accepted_at IS NOT OLD.accepted_at OR
                     NEW.accepted_by IS NOT OLD.accepted_by OR
                     NEW.rejection_reason IS NOT OLD.rejection_reason) AND
                    NOT (OLD.status = 'submitted' AND NEW.status != 'submitted')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'candidate terminal metadata is incoherent');
                END;
                DROP TRIGGER IF EXISTS trg_work_package_node_candidate_verified_output;
                CREATE TRIGGER trg_work_package_node_candidate_verified_output
                BEFORE UPDATE OF status ON work_package_node_candidates
                WHEN NEW.status = 'accepted' AND NOT EXISTS (
                    SELECT 1 FROM evidence_attempt_verifications AS verification
                    WHERE verification.evidence_id = NEW.evidence_id
                      AND verification.task_id = NEW.task_id
                      AND verification.lease_id = NEW.assignment_lease_id
                      AND verification.attempt_number = NEW.attempt_number
                )
                BEGIN
                    SELECT RAISE(ABORT, 'candidate output is not controller-verified');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_node_candidate_no_delete
                BEFORE DELETE ON work_package_node_candidates
                BEGIN
                    SELECT RAISE(ABORT, 'work package candidates are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_task_link_candidate_state
                BEFORE UPDATE OF node_state ON work_package_task_links
                WHEN NEW.node_state IN (
                    'candidate_submitted', 'candidate_accepted',
                    'integrated', 'certified', 'rejected'
                ) AND NOT EXISTS (
                    SELECT 1 FROM work_package_node_candidates AS candidate
                    WHERE candidate.task_id = NEW.task_id
                      AND candidate.package_id = NEW.package_id
                      AND candidate.plan_version = NEW.plan_version
                      AND candidate.epoch = NEW.epoch
                      AND candidate.node_key = NEW.node_key
                      AND (
                          (NEW.node_state = 'candidate_submitted' AND
                           candidate.status = 'submitted') OR
                          (NEW.node_state IN (
                              'candidate_accepted', 'integrated', 'certified'
                           ) AND candidate.status = 'accepted') OR
                          (NEW.node_state = 'rejected' AND
                           candidate.status = 'rejected')
                      )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'node candidate state lacks exact attempt evidence');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_task_link_rework_budget
                BEFORE UPDATE OF node_state ON work_package_task_links
                WHEN OLD.node_state = 'rejected' AND NEW.node_state = 'executing'
                 AND NOT EXISTS (
                    SELECT 1
                    FROM work_package_assignment_audit AS assignment
                    JOIN tasks AS task ON task.id = NEW.task_id
                    WHERE assignment.package_id = NEW.package_id
                      AND assignment.plan_version = NEW.plan_version
                      AND assignment.epoch = NEW.epoch
                      AND assignment.node_key = NEW.node_key
                      AND assignment.task_id = NEW.task_id
                      AND assignment.attempt_number <= task.max_attempts
                      AND assignment.attempt_number > COALESCE((
                          SELECT MAX(candidate.attempt_number)
                          FROM work_package_node_candidates AS candidate
                          WHERE candidate.task_id = NEW.task_id
                            AND candidate.status = 'rejected'
                      ), 0)
                 )
                BEGIN
                    SELECT RAISE(ABORT, 'rework requires a newer bounded assignment');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_lineage_carry_forward_evidence
                BEFORE INSERT ON work_package_node_lineage
                WHEN NEW.relation = 'carried_forward' AND (
                    NEW.source_evidence_id IS NULL OR NOT EXISTS (
                        SELECT 1 FROM work_package_node_candidates AS candidate
                        WHERE candidate.task_id = NEW.from_task_id
                          AND candidate.evidence_id = NEW.source_evidence_id
                          AND candidate.package_id = NEW.package_id
                          AND candidate.plan_version = NEW.from_plan_version
                          AND candidate.epoch = NEW.from_epoch
                          AND candidate.node_key = NEW.from_node_key
                          AND candidate.status = 'accepted'
                    )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'carried-forward lineage requires accepted evidence');
                END;

                -- WIP ownership survives execution-lease expiry.  A token is
                -- transferred through mutation/candidate/fan-in/integration
                -- stages and released only when the candidate is resolved.
                CREATE TABLE IF NOT EXISTS work_package_wip_tokens (
                    id TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    node_key TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    token_kind TEXT NOT NULL,
                    stage TEXT NOT NULL CHECK (stage IN (
                        'mutation', 'candidate_buffer',
                        'fan_in_reservation', 'integration'
                    )),
                    state TEXT NOT NULL CHECK (state IN (
                        'held', 'released', 'superseded', 'cancelled'
                    )),
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    capacity_units INTEGER NOT NULL DEFAULT 1 CHECK (capacity_units >= 1),
                    reservation_key TEXT,
                    predecessor_token_id TEXT REFERENCES work_package_wip_tokens(id)
                        ON DELETE RESTRICT,
                    acquired_by_assignment_lease_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    released_at TEXT,
                    release_reason TEXT,
                    UNIQUE (package_id, epoch, resource_key, generation),
                    FOREIGN KEY (
                        acquired_by_assignment_lease_id, package_id, plan_version,
                        epoch, node_key, task_id
                    ) REFERENCES work_package_assignment_audit (
                        lease_id, package_id, plan_version,
                        epoch, node_key, task_id
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        package_id, plan_version, epoch, node_key, task_id
                    ) REFERENCES work_package_task_links (
                        package_id, plan_version, epoch, node_key, task_id
                    ) ON DELETE RESTRICT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_work_package_held_wip_resource
                    ON work_package_wip_tokens (package_id, epoch, resource_key)
                    WHERE state = 'held';
                CREATE INDEX IF NOT EXISTS idx_work_package_wip_stage
                    ON work_package_wip_tokens (
                        package_id, epoch, stage, state, acquired_at
                    );
                CREATE INDEX IF NOT EXISTS idx_work_package_wip_reservation
                    ON work_package_wip_tokens (reservation_key, state, acquired_at);

                CREATE TRIGGER IF NOT EXISTS trg_work_package_wip_initial_state
                BEFORE INSERT ON work_package_wip_tokens
                WHEN NEW.state != 'held' OR NEW.released_at IS NOT NULL
                 OR NEW.release_reason IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'work package WIP tokens must start held');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_wip_transfer_identity
                BEFORE INSERT ON work_package_wip_tokens
                WHEN NEW.predecessor_token_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM work_package_wip_tokens AS predecessor
                    WHERE predecessor.id = NEW.predecessor_token_id
                      AND predecessor.package_id = NEW.package_id
                      AND predecessor.epoch = NEW.epoch
                      AND predecessor.resource_key = NEW.resource_key
                      AND predecessor.state IN ('released', 'superseded')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'WIP transfer predecessor is not resolved');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_wip_identity_immutable
                BEFORE UPDATE OF
                    id, package_id, plan_version, epoch, node_key, task_id,
                    resource_key, token_kind, stage, generation, capacity_units,
                    reservation_key, predecessor_token_id,
                    acquired_by_assignment_lease_id, acquired_at
                ON work_package_wip_tokens
                BEGIN
                    SELECT RAISE(ABORT, 'work package WIP token identity is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_wip_state_transition
                BEFORE UPDATE OF state ON work_package_wip_tokens
                WHEN NEW.state != OLD.state AND NOT (
                    OLD.state = 'held' AND
                    NEW.state IN ('released', 'superseded', 'cancelled')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid work package WIP state transition');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_wip_release_metadata
                BEFORE UPDATE ON work_package_wip_tokens
                WHEN (
                    NEW.state IN ('released', 'superseded', 'cancelled') AND
                    (NEW.released_at IS NULL OR NEW.release_reason IS NULL OR
                     NEW.release_reason = '')
                ) OR (
                    NEW.state = 'held' AND (
                        NEW.released_at IS NOT NULL OR NEW.release_reason IS NOT NULL
                    )
                ) OR (
                    (NEW.released_at IS NOT OLD.released_at OR
                     NEW.release_reason IS NOT OLD.release_reason) AND
                    NOT (OLD.state = 'held' AND NEW.state != 'held')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'resolved WIP token requires release metadata');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_wip_no_delete
                BEFORE DELETE ON work_package_wip_tokens
                BEGIN
                    SELECT RAISE(ABORT, 'work package WIP tokens are append-only');
                END;

                -- Lease expiry may detach a generic task while its package
                -- node still says ``executing``.  This append-only receipt is
                -- the sole authority for repairing that split state.  It is
                -- authored under the live finalizer fence before the task is
                -- detached, then consumed by the node-transition guards later
                -- in the same transaction.
                CREATE TABLE IF NOT EXISTS work_package_lease_expiry_repairs (
                    id TEXT PRIMARY KEY,
                    lease_id TEXT NOT NULL UNIQUE,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    node_key TEXT NOT NULL,
                    node_generation INTEGER NOT NULL CHECK (node_generation >= 1),
                    task_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                    source_task_state TEXT NOT NULL,
                    target_task_state TEXT NOT NULL CHECK (
                        target_task_state IN ('open', 'waiting', 'failed', 'cancelled')
                    ),
                    source_node_state TEXT NOT NULL DEFAULT 'executing'
                        CHECK (source_node_state = 'executing'),
                    target_node_state TEXT NOT NULL CHECK (
                        target_node_state IN ('ready', 'cancelled')
                    ),
                    wip_disposition TEXT NOT NULL CHECK (
                        wip_disposition IN ('retain', 'cancel')
                    ),
                    held_wip_count INTEGER NOT NULL CHECK (held_wip_count >= 0),
                    held_wip_ids TEXT NOT NULL DEFAULT '[]' CHECK (
                        json_valid(held_wip_ids) AND
                        json_type(held_wip_ids) = 'array' AND
                        json_array_length(held_wip_ids) = held_wip_count
                    ),
                    finalizer_token TEXT NOT NULL CHECK (finalizer_token != ''),
                    decision TEXT NOT NULL CHECK (json_valid(decision)),
                    decision_digest TEXT NOT NULL CHECK (
                        length(decision_digest) = 71 AND
                        decision_digest LIKE 'sha256:%' AND
                        substr(decision_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    reason TEXT NOT NULL CHECK (reason != ''),
                    created_by TEXT NOT NULL CHECK (
                        created_by IN ('controller', 'dispatcher')
                    ),
                    created_at TEXT NOT NULL,
                    CHECK (
                        (
                            target_task_state IN ('open', 'waiting') AND
                            target_node_state = 'ready' AND
                            wip_disposition = 'retain'
                        ) OR (
                            target_task_state IN ('failed', 'cancelled') AND
                            target_node_state = 'cancelled' AND
                            wip_disposition = 'cancel'
                        )
                    ),
                    FOREIGN KEY (
                        lease_id, package_id, plan_version, epoch,
                        node_key, task_id, attempt_number
                    ) REFERENCES work_package_assignment_audit (
                        lease_id, package_id, plan_version, epoch,
                        node_key, task_id, attempt_number
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (lease_id, task_id, agent_id)
                        REFERENCES leases(id, task_id, agent_id) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        package_id, plan_version, epoch, node_key,
                        node_generation, task_id
                    ) REFERENCES work_package_task_links (
                        package_id, plan_version, epoch, node_key,
                        node_generation, task_id
                    ) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_expiry_repairs_node
                    ON work_package_lease_expiry_repairs (
                        package_id, epoch, node_key, attempt_number
                    );
                CREATE TRIGGER IF NOT EXISTS trg_work_package_expiry_repair_authority
                BEFORE INSERT ON work_package_lease_expiry_repairs
                WHEN NOT EXISTS (
                    SELECT 1
                    FROM leases AS lease
                    JOIN tasks AS task ON task.id = lease.task_id
                    JOIN work_package_assignment_audit AS assignment
                      ON assignment.lease_id = lease.id
                    JOIN work_package_task_links AS link
                      ON link.package_id = assignment.package_id
                     AND link.plan_version = assignment.plan_version
                     AND link.epoch = assignment.epoch
                     AND link.node_key = assignment.node_key
                     AND link.task_id = assignment.task_id
                    WHERE lease.id = NEW.lease_id
                      AND lease.task_id = NEW.task_id
                      AND lease.agent_id = NEW.agent_id
                      AND lease.status = 'expired'
                      AND lease.expiry_finalizer_token = NEW.finalizer_token
                      AND lease.expiry_finalized_at IS NULL
                      AND lease.expiry_finalization_decision = NEW.decision
                      AND task.lease_id = NEW.lease_id
                      AND task.owner_agent_id = NEW.agent_id
                      AND task.state = NEW.source_task_state
                      AND assignment.package_id = NEW.package_id
                      AND assignment.plan_version = NEW.plan_version
                      AND assignment.epoch = NEW.epoch
                      AND assignment.node_key = NEW.node_key
                      AND assignment.task_id = NEW.task_id
                      AND assignment.agent_id = NEW.agent_id
                      AND assignment.attempt_number = NEW.attempt_number
                      AND link.node_generation = NEW.node_generation
                      AND link.node_state = NEW.source_node_state
                      AND NEW.attempt_number = (
                          SELECT MAX(latest.attempt_number)
                          FROM work_package_assignment_audit AS latest
                          WHERE latest.package_id = NEW.package_id
                            AND latest.plan_version = NEW.plan_version
                            AND latest.epoch = NEW.epoch
                            AND latest.node_key = NEW.node_key
                            AND latest.task_id = NEW.task_id
                      )
                ) OR (
                    SELECT COUNT(*)
                    FROM work_package_wip_tokens AS token
                    WHERE token.package_id = NEW.package_id
                      AND token.plan_version = NEW.plan_version
                      AND token.epoch = NEW.epoch
                      AND token.node_key = NEW.node_key
                      AND token.task_id = NEW.task_id
                      AND token.state = 'held'
                ) != NEW.held_wip_count OR EXISTS (
                    SELECT 1
                    FROM json_each(NEW.held_wip_ids) AS item
                    WHERE NOT EXISTS (
                        SELECT 1 FROM work_package_wip_tokens AS token
                        WHERE token.id = item.value
                          AND token.package_id = NEW.package_id
                          AND token.plan_version = NEW.plan_version
                          AND token.epoch = NEW.epoch
                          AND token.node_key = NEW.node_key
                          AND token.task_id = NEW.task_id
                          AND token.state = 'held'
                    )
                ) OR EXISTS (
                    SELECT 1
                    FROM work_package_wip_tokens AS token
                    WHERE token.package_id = NEW.package_id
                      AND token.plan_version = NEW.plan_version
                      AND token.epoch = NEW.epoch
                      AND token.node_key = NEW.node_key
                      AND token.task_id = NEW.task_id
                      AND token.state = 'held'
                      AND NOT EXISTS (
                          SELECT 1 FROM json_each(NEW.held_wip_ids) AS item
                          WHERE item.value = token.id
                      )
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'lease-expiry repair lacks exact finalizer or WIP authority'
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_expiry_repairs_immutable
                BEFORE UPDATE ON work_package_lease_expiry_repairs
                BEGIN
                    SELECT RAISE(ABORT, 'lease-expiry repair receipts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_expiry_repairs_no_delete
                BEFORE DELETE ON work_package_lease_expiry_repairs
                BEGIN
                    SELECT RAISE(ABORT, 'lease-expiry repair receipts are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_expiry_task_detach_guard
                BEFORE UPDATE OF lease_id ON tasks
                WHEN OLD.lease_id IS NOT NULL AND NEW.lease_id IS NULL
                 AND EXISTS (
                    SELECT 1
                    FROM leases AS lease
                    JOIN work_package_assignment_audit AS assignment
                      ON assignment.lease_id = lease.id
                    JOIN work_package_task_links AS link
                      ON link.package_id = assignment.package_id
                     AND link.plan_version = assignment.plan_version
                     AND link.epoch = assignment.epoch
                     AND link.node_key = assignment.node_key
                     AND link.task_id = assignment.task_id
                    WHERE lease.id = OLD.lease_id
                      AND lease.task_id = OLD.id
                      AND lease.status = 'expired'
                      AND link.node_state = 'executing'
                 ) AND NOT EXISTS (
                    SELECT 1
                    FROM work_package_lease_expiry_repairs AS repair
                    JOIN leases AS lease ON lease.id = repair.lease_id
                    WHERE repair.lease_id = OLD.lease_id
                      AND repair.task_id = OLD.id
                      AND repair.agent_id = OLD.owner_agent_id
                      AND repair.source_task_state = OLD.state
                      AND repair.target_task_state = NEW.state
                      AND lease.status = 'expired'
                      AND lease.expiry_finalizer_token = repair.finalizer_token
                      AND lease.expiry_finalized_at IS NULL
                      AND NEW.owner_agent_id IS NULL
                 )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'expired package task detach requires exact repair receipt'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_expiry_requeue_guard
                BEFORE UPDATE OF node_state ON work_package_task_links
                WHEN OLD.node_state = 'executing' AND NEW.node_state = 'ready'
                 AND NOT EXISTS (
                    SELECT 1
                    FROM work_package_lease_expiry_repairs AS repair
                    JOIN tasks AS task ON task.id = repair.task_id
                    WHERE repair.package_id = NEW.package_id
                      AND repair.plan_version = NEW.plan_version
                      AND repair.epoch = NEW.epoch
                      AND repair.node_key = NEW.node_key
                      AND repair.node_generation = NEW.node_generation
                      AND repair.task_id = NEW.task_id
                      AND repair.source_node_state = OLD.node_state
                      AND repair.target_node_state = NEW.node_state
                      AND repair.wip_disposition = 'retain'
                      AND task.state = repair.target_task_state
                      AND task.state IN ('open', 'waiting')
                      AND task.lease_id IS NULL
                      AND task.owner_agent_id IS NULL
                      AND repair.attempt_number = (
                          SELECT MAX(latest.attempt_number)
                          FROM work_package_assignment_audit AS latest
                          WHERE latest.package_id = NEW.package_id
                            AND latest.plan_version = NEW.plan_version
                            AND latest.epoch = NEW.epoch
                            AND latest.node_key = NEW.node_key
                            AND latest.task_id = NEW.task_id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM json_each(repair.held_wip_ids) AS item
                          WHERE NOT EXISTS (
                              SELECT 1 FROM work_package_wip_tokens AS token
                              WHERE token.id = item.value
                                AND token.state = 'held'
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM work_package_wip_tokens AS token
                          WHERE token.package_id = NEW.package_id
                            AND token.plan_version = NEW.plan_version
                            AND token.epoch = NEW.epoch
                            AND token.node_key = NEW.node_key
                            AND token.task_id = NEW.task_id
                            AND token.state = 'held'
                            AND NOT EXISTS (
                                SELECT 1
                                FROM json_each(repair.held_wip_ids) AS item
                                WHERE item.value = token.id
                            )
                      )
                 )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'executing node requeue requires exact lease-expiry repair'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_expiry_terminal_wip_guard
                BEFORE UPDATE OF node_state ON work_package_task_links
                WHEN OLD.node_state = 'executing' AND NEW.node_state = 'cancelled'
                 AND EXISTS (
                    SELECT 1 FROM work_package_lease_expiry_repairs AS repair
                    WHERE repair.package_id = NEW.package_id
                      AND repair.plan_version = NEW.plan_version
                      AND repair.epoch = NEW.epoch
                      AND repair.node_key = NEW.node_key
                      AND repair.node_generation = NEW.node_generation
                      AND repair.task_id = NEW.task_id
                      AND repair.target_node_state = 'cancelled'
                 ) AND NOT EXISTS (
                    SELECT 1
                    FROM work_package_lease_expiry_repairs AS repair
                    JOIN tasks AS task ON task.id = repair.task_id
                    WHERE repair.package_id = NEW.package_id
                      AND repair.plan_version = NEW.plan_version
                      AND repair.epoch = NEW.epoch
                      AND repair.node_key = NEW.node_key
                      AND repair.node_generation = NEW.node_generation
                      AND repair.task_id = NEW.task_id
                      AND repair.source_node_state = OLD.node_state
                      AND repair.target_node_state = NEW.node_state
                      AND repair.wip_disposition = 'cancel'
                      AND task.state = repair.target_task_state
                      AND task.state IN ('failed', 'cancelled')
                      AND task.lease_id IS NULL
                      AND task.owner_agent_id IS NULL
                      AND repair.attempt_number = (
                          SELECT MAX(latest.attempt_number)
                          FROM work_package_assignment_audit AS latest
                          WHERE latest.package_id = NEW.package_id
                            AND latest.plan_version = NEW.plan_version
                            AND latest.epoch = NEW.epoch
                            AND latest.node_key = NEW.node_key
                            AND latest.task_id = NEW.task_id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM json_each(repair.held_wip_ids) AS item
                          WHERE NOT EXISTS (
                              SELECT 1 FROM work_package_wip_tokens AS token
                              WHERE token.id = item.value
                                AND token.state = 'cancelled'
                                AND token.released_at IS NOT NULL
                                AND token.release_reason IS NOT NULL
                                AND token.release_reason != ''
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM work_package_wip_tokens AS token
                          WHERE token.package_id = NEW.package_id
                            AND token.plan_version = NEW.plan_version
                            AND token.epoch = NEW.epoch
                            AND token.node_key = NEW.node_key
                            AND token.task_id = NEW.task_id
                            AND token.state = 'held'
                      )
                 )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'terminal lease-expiry repair must cancel exact held WIP'
                    );
                END;

                CREATE TABLE IF NOT EXISTS work_package_integration_batches (
                    id TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    repository_id TEXT REFERENCES project_repositories(id) ON DELETE RESTRICT,
                    target_ref TEXT NOT NULL,
                    assembly_base_sha TEXT NOT NULL,
                    landing_base_sha TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    candidate_sha TEXT,
                    candidate_tree_digest TEXT,
                    candidate_ref TEXT,
                    candidate_fence INTEGER CHECK (
                        candidate_fence IS NULL OR candidate_fence >= 1
                    ),
                    state TEXT NOT NULL CHECK (state IN (
                        'queued', 'assembling', 'verifying', 'certified',
                        'rejected', 'stale', 'published', 'cancelled'
                    )),
                    integration_task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    lease_fence INTEGER NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE (
                        id, package_id, plan_version, epoch,
                        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
                    ),
                    UNIQUE (id, package_id, plan_version, epoch),
                    UNIQUE (id, integration_task_id),
                    CHECK (
                        (lease_owner IS NULL AND lease_expires_at IS NULL) OR
                        (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
                         AND lease_fence >= 1)
                    ),
                    CHECK (
                        (candidate_sha IS NULL AND candidate_tree_digest IS NULL
                         AND candidate_ref IS NULL AND candidate_fence IS NULL) OR
                        (candidate_sha IS NOT NULL AND candidate_tree_digest IS NOT NULL
                         AND candidate_ref IS NOT NULL AND candidate_fence IS NOT NULL
                         AND candidate_ref LIKE 'refs/mac/%')
                    ),
                    FOREIGN KEY (package_id, epoch, plan_version)
                        REFERENCES work_package_epochs(package_id, epoch, plan_version)
                        ON DELETE RESTRICT,
                    FOREIGN KEY (package_id, repository_id)
                        REFERENCES work_packages(id, repository_id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_batches_queue
                    ON work_package_integration_batches (state, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_work_package_batches_target
                    ON work_package_integration_batches (
                        repository_id, target_ref, state, created_at
                    );
                CREATE INDEX IF NOT EXISTS idx_work_package_batches_package
                    ON work_package_integration_batches (package_id, epoch, created_at);

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_repository_insert
                BEFORE INSERT ON work_package_integration_batches
                WHEN COALESCE(NEW.repository_id, '') != COALESCE((
                    SELECT repository_id FROM work_packages WHERE id = NEW.package_id
                ), '')
                BEGIN
                    SELECT RAISE(ABORT, 'integration batch repository must match package');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_repository_update
                BEFORE UPDATE OF package_id, repository_id ON work_package_integration_batches
                WHEN COALESCE(NEW.repository_id, '') != COALESCE((
                    SELECT repository_id FROM work_packages WHERE id = NEW.package_id
                ), '')
                BEGIN
                    SELECT RAISE(ABORT, 'integration batch repository must match package');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_fence_monotonic
                BEFORE UPDATE OF lease_fence ON work_package_integration_batches
                WHEN NEW.lease_fence < OLD.lease_fence
                BEGIN
                    SELECT RAISE(ABORT, 'integration batch lease fence cannot decrease');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_fence_owner_change
                BEFORE UPDATE OF lease_owner, lease_fence
                ON work_package_integration_batches
                WHEN NEW.lease_owner IS NOT NULL
                 AND NEW.lease_owner IS NOT OLD.lease_owner
                 AND NEW.lease_fence <= OLD.lease_fence
                BEGIN
                    SELECT RAISE(ABORT, 'integration batch owner change requires a new fence');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_identity_immutable
                BEFORE UPDATE OF
                    id, package_id, plan_version, epoch, repository_id, target_ref,
                    assembly_base_sha, landing_base_sha, input_digest,
                    integration_task_id, created_at
                ON work_package_integration_batches
                BEGIN
                    SELECT RAISE(ABORT, 'integration batch identity is immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_candidate_assignment
                BEFORE UPDATE OF
                    candidate_sha, candidate_tree_digest, candidate_ref, candidate_fence
                ON work_package_integration_batches
                WHEN NOT (
                    OLD.state = 'assembling' AND NEW.state = 'assembling' AND
                    OLD.candidate_sha IS NULL AND OLD.candidate_tree_digest IS NULL AND
                    OLD.candidate_ref IS NULL AND OLD.candidate_fence IS NULL AND
                    NEW.candidate_sha IS NOT NULL AND
                    NEW.candidate_tree_digest IS NOT NULL AND
                    NEW.candidate_ref LIKE 'refs/mac/%' AND
                    NEW.candidate_fence = OLD.lease_fence AND
                    OLD.lease_owner IS NOT NULL AND
                    NEW.lease_owner IS OLD.lease_owner AND
                    NEW.lease_fence = OLD.lease_fence
                )
                BEGIN
                    SELECT RAISE(ABORT, 'integration candidate assignment requires current fence');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_state_transition
                BEFORE UPDATE OF state ON work_package_integration_batches
                WHEN NEW.state != OLD.state AND NOT (
                    (OLD.state = 'queued' AND NEW.state IN ('assembling', 'cancelled')) OR
                    (OLD.state = 'assembling' AND NEW.state IN (
                        'verifying', 'rejected', 'stale', 'cancelled'
                    )) OR
                    (OLD.state = 'verifying' AND NEW.state IN (
                        'certified', 'rejected', 'stale', 'cancelled'
                    )) OR
                    (OLD.state = 'certified' AND NEW.state IN ('published', 'stale'))
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid integration batch state transition');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_verify_candidate
                BEFORE UPDATE OF state ON work_package_integration_batches
                WHEN NEW.state = 'verifying' AND (
                    NEW.candidate_sha IS NULL OR NEW.candidate_tree_digest IS NULL OR
                    NEW.candidate_ref IS NULL OR NEW.candidate_fence IS NULL
                )
                BEGIN
                    SELECT RAISE(ABORT, 'verifying batch requires a fenced candidate');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_initial_state
                BEFORE INSERT ON work_package_integration_batches
                WHEN NEW.state != 'queued' OR NEW.candidate_sha IS NOT NULL
                 OR NEW.candidate_tree_digest IS NOT NULL OR NEW.candidate_ref IS NOT NULL
                 OR NEW.candidate_fence IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'integration batches must start queued');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_no_delete
                BEFORE DELETE ON work_package_integration_batches
                BEGIN
                    SELECT RAISE(ABORT, 'integration batches are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_batch_inputs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                    node_key TEXT NOT NULL,
                    node_generation INTEGER NOT NULL CHECK (node_generation >= 1),
                    task_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    candidate_status TEXT NOT NULL DEFAULT 'accepted'
                        CHECK (candidate_status = 'accepted'),
                    assignment_lease_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                    evidence_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (batch_id, ordinal),
                    UNIQUE (batch_id, evidence_id),
                    FOREIGN KEY (batch_id, package_id, plan_version, epoch)
                        REFERENCES work_package_integration_batches(
                            id, package_id, plan_version, epoch
                        ) ON DELETE CASCADE,
                    FOREIGN KEY (
                        assignment_lease_id, package_id, plan_version, epoch,
                        node_key, task_id, attempt_number
                    ) REFERENCES work_package_assignment_audit (
                        lease_id, package_id, plan_version, epoch,
                        node_key, task_id, attempt_number
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (evidence_id, task_id)
                        REFERENCES evidence(id, task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        evidence_id, task_id, assignment_lease_id, attempt_number
                    ) REFERENCES evidence_attempt_links (
                        evidence_id, task_id, lease_id, attempt_number
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        package_id, plan_version, epoch, node_key, task_id
                    ) REFERENCES work_package_task_links (
                        package_id, plan_version, epoch, node_key, task_id
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        candidate_id, task_id, package_id, plan_version, epoch,
                        node_key, node_generation, assignment_lease_id,
                        attempt_number, evidence_id, candidate_status
                    ) REFERENCES work_package_node_candidates (
                        id, task_id, package_id, plan_version, epoch,
                        node_key, node_generation, assignment_lease_id,
                        attempt_number, evidence_id, status
                    ) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_batch_inputs_task
                    ON work_package_batch_inputs (task_id, created_at);

                DROP TRIGGER IF EXISTS trg_work_package_batch_inputs_insert_open;
                CREATE TRIGGER trg_work_package_batch_inputs_insert_open
                BEFORE INSERT ON work_package_batch_inputs
                WHEN COALESCE((
                    SELECT state FROM work_package_integration_batches
                    WHERE id = NEW.batch_id
                ), '') != 'queued' OR NOT EXISTS (
                    SELECT 1 FROM work_package_task_links AS link
                    WHERE link.task_id = NEW.task_id
                      AND link.package_id = NEW.package_id
                      AND link.plan_version = NEW.plan_version
                      AND link.epoch = NEW.epoch
                      AND link.node_key = NEW.node_key
                      AND link.node_generation = NEW.node_generation
                      AND link.node_state = 'candidate_accepted'
                ) OR NOT EXISTS (
                    SELECT 1 FROM evidence_attempt_verifications AS verification
                    WHERE verification.evidence_id = NEW.evidence_id
                      AND verification.task_id = NEW.task_id
                      AND verification.lease_id = NEW.assignment_lease_id
                      AND verification.attempt_number = NEW.attempt_number
                )
                BEGIN
                    SELECT RAISE(ABORT, 'batch input is not an accepted verified candidate');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_inputs_update_open
                BEFORE UPDATE ON work_package_batch_inputs
                WHEN COALESCE((
                    SELECT state FROM work_package_integration_batches
                    WHERE id = OLD.batch_id
                ), '') != 'queued'
                BEGIN
                    SELECT RAISE(ABORT, 'batch membership is immutable after assembly starts');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_batch_inputs_delete_open
                BEFORE DELETE ON work_package_batch_inputs
                WHEN COALESCE((
                    SELECT state FROM work_package_integration_batches
                    WHERE id = OLD.batch_id
                ), '') != 'queued'
                BEGIN
                    SELECT RAISE(ABORT, 'batch membership is immutable after assembly starts');
                END;

                CREATE UNIQUE INDEX IF NOT EXISTS uniq_publications_task_evidence_identity
                    ON publications (id, task_id, evidence_id);
                CREATE TABLE IF NOT EXISTS work_package_certifications (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    candidate_sha TEXT NOT NULL,
                    assembly_base_sha TEXT NOT NULL,
                    landing_base_sha TEXT NOT NULL,
                    target_ref TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN (
                        'passed', 'failed', 'invalidated', 'published'
                    )),
                    verification_digest TEXT NOT NULL,
                    verification TEXT NOT NULL DEFAULT '{}',
                    certification_task_id TEXT NOT NULL,
                    tests_evidence_id TEXT NOT NULL,
                    review_task_id TEXT NOT NULL,
                    review_evidence_id TEXT NOT NULL,
                    codegraph_evidence_id TEXT,
                    certified_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    invalidated_at TEXT,
                    publication_id TEXT,
                    publication_evidence_id TEXT,
                    UNIQUE (batch_id, candidate_sha, verification_digest),
                    CHECK (
                        (publication_id IS NULL AND publication_evidence_id IS NULL) OR
                        (publication_id IS NOT NULL AND publication_evidence_id IS NOT NULL)
                    ),
                    FOREIGN KEY (
                        batch_id, package_id, plan_version, epoch,
                        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
                    ) REFERENCES work_package_integration_batches (
                        id, package_id, plan_version, epoch,
                        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (tests_evidence_id, certification_task_id)
                        REFERENCES evidence(id, task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (review_evidence_id, review_task_id)
                        REFERENCES evidence(id, task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (codegraph_evidence_id, certification_task_id)
                        REFERENCES evidence(id, task_id) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        publication_id, certification_task_id, publication_evidence_id
                    ) REFERENCES publications (id, task_id, evidence_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_certifications_status
                    ON work_package_certifications (status, created_at, id);
                CREATE INDEX IF NOT EXISTS idx_work_package_certifications_package
                    ON work_package_certifications (package_id, epoch, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_work_package_cert_landing_identity
                    ON work_package_certifications (
                        id, batch_id, package_id, plan_version, epoch, candidate_sha,
                        assembly_base_sha, landing_base_sha, target_ref
                    );

                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_initial_state
                BEFORE INSERT ON work_package_certifications
                WHEN NEW.status NOT IN ('passed', 'failed') OR
                     NEW.invalidated_at IS NOT NULL OR
                     NEW.publication_id IS NOT NULL OR
                     NEW.publication_evidence_id IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'certification must start uncommitted as passed or failed');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_batch_ready
                BEFORE INSERT ON work_package_certifications
                WHEN NOT EXISTS (
                    SELECT 1 FROM work_package_integration_batches AS batch
                    WHERE batch.id = NEW.batch_id
                      AND batch.package_id = NEW.package_id
                      AND batch.plan_version = NEW.plan_version
                      AND batch.epoch = NEW.epoch
                      AND batch.state = 'verifying'
                      AND batch.candidate_sha = NEW.candidate_sha
                      AND batch.assembly_base_sha = NEW.assembly_base_sha
                      AND batch.landing_base_sha = NEW.landing_base_sha
                      AND batch.target_ref = NEW.target_ref
                      AND batch.candidate_tree_digest IS NOT NULL
                      AND batch.candidate_ref IS NOT NULL
                      AND batch.candidate_fence IS NOT NULL
                )
                BEGIN
                    SELECT RAISE(ABORT, 'certification requires a finalized verifying batch');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_identity
                BEFORE UPDATE OF
                    id, batch_id, package_id, plan_version, epoch, candidate_sha,
                    assembly_base_sha, landing_base_sha, target_ref,
                    verification_digest, verification, certification_task_id,
                    tests_evidence_id, review_task_id, review_evidence_id,
                    codegraph_evidence_id, certified_by, created_at
                ON work_package_certifications
                BEGIN
                    SELECT RAISE(ABORT, 'certification identity is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_state
                BEFORE UPDATE OF status ON work_package_certifications
                WHEN NEW.status != OLD.status AND NOT (
                    (OLD.status = 'passed' AND NEW.status IN ('invalidated', 'published')) OR
                    (OLD.status = 'failed' AND NEW.status = 'invalidated')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid certification state transition');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_metadata
                BEFORE UPDATE ON work_package_certifications
                WHEN (
                    NEW.status = 'invalidated' AND (
                        NEW.invalidated_at IS NULL OR
                        NEW.publication_id IS NOT NULL OR
                        NEW.publication_evidence_id IS NOT NULL
                    )
                ) OR (
                    NEW.status = 'published' AND (
                        NEW.invalidated_at IS NOT NULL OR
                        NEW.publication_id IS NULL OR
                        NEW.publication_evidence_id IS NULL
                    )
                ) OR (
                    NEW.status IN ('passed', 'failed') AND (
                        NEW.invalidated_at IS NOT NULL OR
                        NEW.publication_id IS NOT NULL OR
                        NEW.publication_evidence_id IS NOT NULL
                    )
                ) OR (
                    NEW.invalidated_at IS NOT OLD.invalidated_at AND
                    NOT (OLD.status IN ('passed', 'failed') AND NEW.status = 'invalidated')
                ) OR (
                    (NEW.publication_id IS NOT OLD.publication_id OR
                     NEW.publication_evidence_id IS NOT OLD.publication_evidence_id)
                    AND NOT (OLD.status = 'passed' AND NEW.status = 'published')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'certification terminal metadata is incoherent');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_no_delete
                BEFORE DELETE ON work_package_certifications
                BEGIN
                    SELECT RAISE(ABORT, 'certifications are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_certification_jobs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL UNIQUE,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
                    epoch INTEGER NOT NULL CHECK (epoch >= 1),
                    repository_id TEXT NOT NULL,
                    candidate_sha TEXT NOT NULL CHECK (
                        length(candidate_sha) = 40 AND
                        candidate_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    candidate_tree_digest TEXT NOT NULL CHECK (
                        length(candidate_tree_digest) = 49 AND
                        candidate_tree_digest LIKE 'git-tree:%' AND
                        substr(candidate_tree_digest, 10) NOT GLOB '*[^0-9a-f]*'
                    ),
                    candidate_ref TEXT NOT NULL CHECK (
                        candidate_ref LIKE 'refs/mac/%'
                    ),
                    candidate_fence INTEGER NOT NULL CHECK (candidate_fence >= 1),
                    assembly_base_sha TEXT NOT NULL CHECK (
                        length(assembly_base_sha) = 40 AND
                        assembly_base_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    landing_base_sha TEXT NOT NULL CHECK (
                        length(landing_base_sha) = 40 AND
                        landing_base_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    target_ref TEXT NOT NULL CHECK (target_ref LIKE 'refs/heads/%'),
                    policy_id TEXT NOT NULL CHECK (policy_id != ''),
                    policy_version INTEGER NOT NULL CHECK (policy_version >= 1),
                    policy_checksum TEXT NOT NULL CHECK (
                        length(policy_checksum) = 71 AND
                        policy_checksum LIKE 'sha256:%' AND
                        substr(policy_checksum, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    image_ref TEXT NOT NULL CHECK (image_ref LIKE '%@' || image_digest),
                    image_digest TEXT NOT NULL CHECK (
                        length(image_digest) = 71 AND image_digest LIKE 'sha256:%' AND
                        substr(image_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    bundle_digest TEXT NOT NULL CHECK (
                        length(bundle_digest) = 71 AND bundle_digest LIKE 'sha256:%' AND
                        substr(bundle_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    commands_digest TEXT NOT NULL CHECK (
                        length(commands_digest) = 71 AND commands_digest LIKE 'sha256:%' AND
                        substr(commands_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    job_digest TEXT NOT NULL UNIQUE CHECK (
                        length(job_digest) = 71 AND job_digest LIKE 'sha256:%' AND
                        substr(job_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    definition TEXT NOT NULL CHECK (
                        json_valid(definition) AND json_type(definition) = 'object'
                    ),
                    state TEXT NOT NULL CHECK (state IN (
                        'queued', 'running', 'completed', 'failed'
                    )),
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    lease_fence INTEGER NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
                    result_digest TEXT UNIQUE CHECK (
                        result_digest IS NULL OR (
                            length(result_digest) = 71 AND
                            result_digest LIKE 'sha256:%' AND
                            substr(result_digest, 8) NOT GLOB '*[^0-9a-f]*'
                        )
                    ),
                    certification_id TEXT UNIQUE
                        REFERENCES work_package_certifications(id) ON DELETE RESTRICT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE (
                        id, batch_id, package_id, plan_version, epoch,
                        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
                    ),
                    FOREIGN KEY (
                        batch_id, package_id, plan_version, epoch, candidate_sha,
                        assembly_base_sha, landing_base_sha, target_ref
                    ) REFERENCES work_package_integration_batches (
                        id, package_id, plan_version, epoch, candidate_sha,
                        assembly_base_sha, landing_base_sha, target_ref
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (repository_id)
                        REFERENCES project_repositories(id) ON DELETE RESTRICT,
                    CHECK (
                        (state = 'queued' AND lease_owner IS NULL
                         AND lease_expires_at IS NULL AND lease_fence = 0
                         AND result_digest IS NULL AND certification_id IS NULL
                         AND completed_at IS NULL) OR
                        (state = 'running' AND lease_owner IS NOT NULL
                         AND lease_expires_at IS NOT NULL AND lease_fence >= 1
                         AND result_digest IS NULL AND certification_id IS NULL
                         AND completed_at IS NULL) OR
                        (state IN ('completed', 'failed') AND result_digest IS NOT NULL
                         AND certification_id IS NOT NULL AND completed_at IS NOT NULL
                         AND lease_owner IS NULL AND lease_expires_at IS NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_certification_jobs_state
                    ON work_package_certification_jobs (state, created_at, id);
                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_job_initial
                BEFORE INSERT ON work_package_certification_jobs
                WHEN NEW.state != 'queued' OR NEW.lease_owner IS NOT NULL
                 OR NEW.lease_expires_at IS NOT NULL OR NEW.lease_fence != 0
                 OR NEW.result_digest IS NOT NULL OR NEW.certification_id IS NOT NULL
                 OR NEW.completed_at IS NOT NULL
                BEGIN
                    SELECT RAISE(ABORT, 'certification jobs must start queued');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_job_identity
                BEFORE UPDATE OF
                    id, batch_id, package_id, plan_version, epoch, repository_id,
                    candidate_sha, candidate_tree_digest, candidate_ref,
                    candidate_fence, assembly_base_sha, landing_base_sha, target_ref,
                    policy_id, policy_version, policy_checksum, image_ref,
                    image_digest, bundle_digest, commands_digest, job_digest,
                    definition, created_at
                ON work_package_certification_jobs
                BEGIN
                    SELECT RAISE(ABORT, 'certification job identity is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_job_state
                BEFORE UPDATE OF state ON work_package_certification_jobs
                WHEN NEW.state != OLD.state AND NOT (
                    OLD.state = 'queued' AND NEW.state = 'running' OR
                    OLD.state = 'running' AND NEW.state IN ('completed', 'failed')
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid certification job state transition');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_job_fence
                BEFORE UPDATE OF lease_fence ON work_package_certification_jobs
                WHEN NEW.lease_fence NOT IN (OLD.lease_fence, OLD.lease_fence + 1) OR (
                    NEW.lease_owner IS NOT NULL AND
                    NEW.lease_owner IS NOT OLD.lease_owner AND
                    NEW.lease_fence <= OLD.lease_fence
                )
                BEGIN
                    SELECT RAISE(ABORT, 'certification job owner requires a new fence');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_job_result
                BEFORE UPDATE OF result_digest, certification_id
                ON work_package_certification_jobs
                WHEN NEW.certification_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM work_package_certifications AS certification
                    WHERE certification.id = NEW.certification_id
                      AND certification.batch_id = NEW.batch_id
                      AND certification.package_id = NEW.package_id
                      AND certification.plan_version = NEW.plan_version
                      AND certification.epoch = NEW.epoch
                      AND certification.candidate_sha = NEW.candidate_sha
                      AND certification.assembly_base_sha = NEW.assembly_base_sha
                      AND certification.landing_base_sha = NEW.landing_base_sha
                      AND certification.target_ref = NEW.target_ref
                      AND certification.verification_digest = NEW.result_digest
                )
                BEGIN
                    SELECT RAISE(ABORT, 'certification job result identity is invalid');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_certification_job_no_delete
                BEFORE DELETE ON work_package_certification_jobs
                BEGIN
                    SELECT RAISE(ABORT, 'certification jobs are append-only');
                END;

                -- Controller-owned integration and certification nodes never
                -- manufacture worker candidates.  Their terminal graph state
                -- is authorized instead by one immutable receipt bound to the
                -- exact batch/job/certification that the controller observed.
                CREATE TABLE IF NOT EXISTS work_package_controller_station_receipts (
                    id TEXT PRIMARY KEY,
                    station_kind TEXT NOT NULL CHECK (
                        station_kind IN ('integration', 'certification')
                    ),
                    task_id TEXT NOT NULL UNIQUE,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
                    epoch INTEGER NOT NULL CHECK (epoch >= 1),
                    node_key TEXT NOT NULL CHECK (node_key != ''),
                    batch_id TEXT NOT NULL,
                    certification_job_id TEXT UNIQUE
                        REFERENCES work_package_certification_jobs(id) ON DELETE RESTRICT,
                    certification_id TEXT UNIQUE
                        REFERENCES work_package_certifications(id) ON DELETE RESTRICT,
                    outcome TEXT NOT NULL CHECK (
                        outcome IN ('integrated', 'certified', 'rejected')
                    ),
                    result_digest TEXT CHECK (
                        result_digest IS NULL OR (
                            length(result_digest) = 71 AND
                            result_digest LIKE 'sha256:%' AND
                            substr(result_digest, 8) NOT GLOB '*[^0-9a-f]*'
                        )
                    ),
                    provenance_digest TEXT NOT NULL UNIQUE CHECK (
                        length(provenance_digest) = 71 AND
                        provenance_digest LIKE 'sha256:%' AND
                        substr(provenance_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    actor TEXT NOT NULL CHECK (actor != ''),
                    detail TEXT NOT NULL CHECK (
                        json_valid(detail) AND json_type(detail) = 'object'
                    ),
                    created_at TEXT NOT NULL,
                    UNIQUE (package_id, plan_version, epoch, node_key),
                    FOREIGN KEY (
                        package_id, plan_version, epoch, node_key, task_id
                    ) REFERENCES work_package_task_links (
                        package_id, plan_version, epoch, node_key, task_id
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (batch_id, package_id, plan_version, epoch)
                        REFERENCES work_package_integration_batches (
                            id, package_id, plan_version, epoch
                        ) ON DELETE RESTRICT,
                    CHECK (
                        (station_kind = 'integration' AND outcome = 'integrated'
                         AND certification_job_id IS NULL
                         AND certification_id IS NULL AND result_digest IS NULL) OR
                        (station_kind = 'certification'
                         AND outcome IN ('certified', 'rejected')
                         AND certification_job_id IS NOT NULL
                         AND certification_id IS NOT NULL
                         AND result_digest IS NOT NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_controller_station_batch
                    ON work_package_controller_station_receipts (
                        batch_id, station_kind, outcome, created_at
                    );
                CREATE TRIGGER IF NOT EXISTS trg_work_package_controller_station_exact
                BEFORE INSERT ON work_package_controller_station_receipts
                WHEN NOT (
                    (
                        NEW.station_kind = 'integration' AND
                        EXISTS (
                            SELECT 1
                            FROM work_package_integration_batches AS batch
                            JOIN work_package_task_links AS link
                              ON link.task_id = NEW.task_id
                             AND link.package_id = NEW.package_id
                             AND link.plan_version = NEW.plan_version
                             AND link.epoch = NEW.epoch
                             AND link.node_key = NEW.node_key
                            JOIN tasks AS task ON task.id = link.task_id
                            WHERE batch.id = NEW.batch_id
                              AND batch.package_id = NEW.package_id
                              AND batch.plan_version = NEW.plan_version
                              AND batch.epoch = NEW.epoch
                              AND batch.integration_task_id = NEW.task_id
                              AND batch.state = 'verifying'
                              AND batch.candidate_sha IS NOT NULL
                              AND batch.candidate_tree_digest IS NOT NULL
                              AND batch.candidate_ref IS NOT NULL
                              AND batch.candidate_fence IS NOT NULL
                              AND link.node_state IN ('planned', 'ready')
                              AND task.state IN ('open', 'waiting')
                              AND task.owner_agent_id IS NULL
                              AND task.lease_id IS NULL
                              AND json_extract(task.metadata, '$.no_dispatch') = 1
                              AND json_extract(
                                  task.metadata,
                                  '$.work_package.node_type'
                              ) = 'integration'
                        )
                    ) OR (
                        NEW.station_kind = 'certification' AND
                        EXISTS (
                            SELECT 1
                            FROM work_package_certification_jobs AS job
                            JOIN work_package_certifications AS certification
                              ON certification.id = NEW.certification_id
                             AND certification.batch_id = job.batch_id
                             AND certification.package_id = job.package_id
                             AND certification.plan_version = job.plan_version
                             AND certification.epoch = job.epoch
                             AND certification.candidate_sha = job.candidate_sha
                             AND certification.assembly_base_sha = job.assembly_base_sha
                             AND certification.landing_base_sha = job.landing_base_sha
                             AND certification.target_ref = job.target_ref
                             AND certification.verification_digest = job.result_digest
                            JOIN work_package_task_links AS link
                              ON link.task_id = NEW.task_id
                             AND link.package_id = NEW.package_id
                             AND link.plan_version = NEW.plan_version
                             AND link.epoch = NEW.epoch
                             AND link.node_key = NEW.node_key
                            JOIN tasks AS task ON task.id = link.task_id
                            WHERE job.id = NEW.certification_job_id
                              AND job.batch_id = NEW.batch_id
                              AND job.package_id = NEW.package_id
                              AND job.plan_version = NEW.plan_version
                              AND job.epoch = NEW.epoch
                              AND job.certification_id = NEW.certification_id
                              AND job.result_digest = NEW.result_digest
                              AND json_extract(
                                  job.definition,
                                  '$.certification_task_id'
                              ) = NEW.task_id
                              AND json_extract(
                                  job.definition,
                                  '$.certification_node_key'
                              ) = NEW.node_key
                              AND (
                                  (NEW.outcome = 'certified'
                                   AND job.state = 'completed'
                                   AND certification.status = 'passed') OR
                                  (NEW.outcome = 'rejected'
                                   AND job.state = 'failed'
                                   AND certification.status = 'failed')
                              )
                              AND link.node_state = 'ready'
                              AND task.state = 'waiting'
                              AND task.owner_agent_id IS NULL
                              AND task.lease_id IS NULL
                              AND certification.certification_task_id = NEW.task_id
                              AND json_extract(task.metadata, '$.no_dispatch') = 1
                              AND json_extract(
                                  task.metadata,
                                  '$.work_package.node_type'
                              ) = 'certification'
                        )
                    )
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'controller station receipt lacks exact durable provenance'
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_controller_station_immutable
                BEFORE UPDATE ON work_package_controller_station_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'controller station receipts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_controller_station_no_delete
                BEFORE DELETE ON work_package_controller_station_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'controller station receipts are append-only');
                END;

                -- Replace the generic worker-candidate guards with receipt-aware
                -- forms.  Ordinary worker transitions keep their exact candidate
                -- requirements; only a matching controller receipt authorizes a
                -- direct controller-station terminal projection.
                DROP TRIGGER IF EXISTS trg_work_package_task_links_state_transition;
                CREATE TRIGGER trg_work_package_task_links_state_transition
                BEFORE UPDATE OF node_state ON work_package_task_links
                WHEN NEW.node_state != OLD.node_state AND NOT (
                    (OLD.node_state = 'planned' AND NEW.node_state IN (
                        'ready', 'superseded', 'cancelled'
                    )) OR
                    (OLD.node_state = 'ready' AND NEW.node_state IN (
                        'executing', 'superseded', 'cancelled'
                    )) OR
                    (OLD.node_state = 'executing' AND NEW.node_state IN (
                        'ready', 'candidate_submitted', 'rejected', 'cancelled'
                    )) OR
                    (OLD.node_state = 'candidate_submitted' AND NEW.node_state IN (
                        'candidate_accepted', 'rejected', 'superseded'
                    )) OR
                    (OLD.node_state = 'candidate_accepted' AND NEW.node_state IN (
                        'integrated', 'rejected', 'superseded'
                    )) OR
                    (OLD.node_state = 'integrated' AND NEW.node_state IN (
                        'certified', 'rejected', 'superseded'
                    )) OR
                    (OLD.node_state = 'rejected' AND NEW.node_state = 'executing') OR
                    (
                        OLD.node_state IN ('planned', 'ready') AND
                        NEW.node_state = 'integrated' AND EXISTS (
                            SELECT 1
                            FROM work_package_controller_station_receipts AS receipt
                            WHERE receipt.task_id = NEW.task_id
                              AND receipt.package_id = NEW.package_id
                              AND receipt.plan_version = NEW.plan_version
                              AND receipt.epoch = NEW.epoch
                              AND receipt.node_key = NEW.node_key
                              AND receipt.station_kind = 'integration'
                              AND receipt.outcome = 'integrated'
                        )
                    ) OR (
                        OLD.node_state = 'ready' AND
                        NEW.node_state IN ('certified', 'rejected') AND EXISTS (
                            SELECT 1
                            FROM work_package_controller_station_receipts AS receipt
                            WHERE receipt.task_id = NEW.task_id
                              AND receipt.package_id = NEW.package_id
                              AND receipt.plan_version = NEW.plan_version
                              AND receipt.epoch = NEW.epoch
                              AND receipt.node_key = NEW.node_key
                              AND receipt.station_kind = 'certification'
                              AND receipt.outcome = NEW.node_state
                        )
                    )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid work package node state transition');
                END;

                DROP TRIGGER IF EXISTS trg_work_package_task_link_candidate_state;
                CREATE TRIGGER trg_work_package_task_link_candidate_state
                BEFORE UPDATE OF node_state ON work_package_task_links
                WHEN NEW.node_state IN (
                    'candidate_submitted', 'candidate_accepted',
                    'integrated', 'certified', 'rejected'
                ) AND NOT EXISTS (
                    SELECT 1 FROM work_package_node_candidates AS candidate
                    WHERE candidate.task_id = NEW.task_id
                      AND candidate.package_id = NEW.package_id
                      AND candidate.plan_version = NEW.plan_version
                      AND candidate.epoch = NEW.epoch
                      AND candidate.node_key = NEW.node_key
                      AND (
                          (NEW.node_state = 'candidate_submitted' AND
                           candidate.status = 'submitted') OR
                          (NEW.node_state IN (
                              'candidate_accepted', 'integrated', 'certified'
                           ) AND candidate.status = 'accepted') OR
                          (NEW.node_state = 'rejected' AND
                           candidate.status = 'rejected')
                      )
                ) AND NOT EXISTS (
                    SELECT 1
                    FROM work_package_controller_station_receipts AS receipt
                    WHERE receipt.task_id = NEW.task_id
                      AND receipt.package_id = NEW.package_id
                      AND receipt.plan_version = NEW.plan_version
                      AND receipt.epoch = NEW.epoch
                      AND receipt.node_key = NEW.node_key
                      AND receipt.outcome = NEW.node_state
                )
                BEGIN
                    SELECT RAISE(ABORT, 'node terminal state lacks exact provenance');
                END;

                CREATE TABLE IF NOT EXISTS work_package_landing_streams (
                    repository_id TEXT NOT NULL
                        REFERENCES project_repositories(id) ON DELETE RESTRICT,
                    target_ref TEXT NOT NULL CHECK (target_ref LIKE 'refs/heads/%'),
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    lease_fence INTEGER NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (repository_id, target_ref),
                    CHECK (
                        (lease_owner IS NULL AND lease_expires_at IS NULL) OR
                        (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
                         AND lease_fence >= 1)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_landing_stream_lease
                    ON work_package_landing_streams (lease_expires_at, repository_id);

                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_stream_identity
                BEFORE UPDATE OF repository_id, target_ref, created_at
                ON work_package_landing_streams
                BEGIN
                    SELECT RAISE(ABORT, 'landing stream identity is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_stream_fence
                BEFORE UPDATE OF lease_fence ON work_package_landing_streams
                WHEN NEW.lease_fence < OLD.lease_fence OR (
                    NEW.lease_owner IS NOT NULL AND
                    NEW.lease_owner IS NOT OLD.lease_owner AND
                    NEW.lease_fence <= OLD.lease_fence
                )
                BEGIN
                    SELECT RAISE(ABORT, 'landing stream owner requires a monotonic fence');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_stream_no_delete
                BEFORE DELETE ON work_package_landing_streams
                BEGIN
                    SELECT RAISE(ABORT, 'landing streams are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_landing_intents (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL UNIQUE,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    epoch INTEGER NOT NULL,
                    repository_id TEXT NOT NULL,
                    target_ref TEXT NOT NULL,
                    candidate_sha TEXT NOT NULL CHECK (
                        length(candidate_sha) = 40 AND
                        candidate_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    candidate_ref TEXT NOT NULL CHECK (candidate_ref LIKE 'refs/mac/%'),
                    assembly_base_sha TEXT NOT NULL CHECK (
                        length(assembly_base_sha) = 40 AND
                        assembly_base_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    landing_base_sha TEXT NOT NULL CHECK (
                        length(landing_base_sha) = 40 AND
                        landing_base_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    certification_id TEXT NOT NULL UNIQUE,
                    stream_fence INTEGER NOT NULL CHECK (stream_fence >= 1),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (
                        id, repository_id, target_ref, candidate_sha,
                        landing_base_sha
                    ),
                    UNIQUE (
                        id, batch_id, repository_id, target_ref, candidate_sha
                    ),
                    FOREIGN KEY (repository_id, target_ref)
                        REFERENCES work_package_landing_streams(
                            repository_id, target_ref
                        ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        batch_id, package_id, plan_version, epoch, candidate_sha,
                        assembly_base_sha, landing_base_sha, target_ref
                    ) REFERENCES work_package_integration_batches (
                        id, package_id, plan_version, epoch, candidate_sha,
                        assembly_base_sha, landing_base_sha, target_ref
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        certification_id, batch_id, package_id, plan_version, epoch,
                        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
                    ) REFERENCES work_package_certifications (
                        id, batch_id, package_id, plan_version, epoch, candidate_sha,
                        assembly_base_sha, landing_base_sha, target_ref
                    ) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_landing_intents_target
                    ON work_package_landing_intents (
                        repository_id, target_ref, created_at, id
                    );

                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_intent_ready
                BEFORE INSERT ON work_package_landing_intents
                WHEN NOT EXISTS (
                    SELECT 1 FROM work_package_integration_batches AS batch
                    JOIN work_package_certifications AS certification
                      ON certification.id = NEW.certification_id
                     AND certification.batch_id = batch.id
                    JOIN work_package_landing_streams AS stream
                      ON stream.repository_id = NEW.repository_id
                     AND stream.target_ref = NEW.target_ref
                    WHERE batch.id = NEW.batch_id
                      AND batch.package_id = NEW.package_id
                      AND batch.plan_version = NEW.plan_version
                      AND batch.epoch = NEW.epoch
                      AND batch.repository_id = NEW.repository_id
                      AND batch.target_ref = NEW.target_ref
                      AND batch.candidate_sha = NEW.candidate_sha
                      AND batch.candidate_ref = NEW.candidate_ref
                      AND batch.assembly_base_sha = NEW.assembly_base_sha
                      AND batch.landing_base_sha = NEW.landing_base_sha
                      AND batch.state = 'certified'
                      AND certification.status = 'passed'
                      AND stream.lease_owner = NEW.created_by
                      AND stream.lease_fence = NEW.stream_fence
                      AND stream.lease_expires_at > NEW.created_at
                )
                BEGIN
                    SELECT RAISE(ABORT, 'landing intent requires an exact certified candidate and current stream fence');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_intent_immutable
                BEFORE UPDATE ON work_package_landing_intents
                BEGIN
                    SELECT RAISE(ABORT, 'landing intents are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_intent_no_delete
                BEFORE DELETE ON work_package_landing_intents
                BEGIN
                    SELECT RAISE(ABORT, 'landing intents are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_landing_attempts (
                    id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                    repository_id TEXT NOT NULL,
                    target_ref TEXT NOT NULL,
                    candidate_sha TEXT NOT NULL CHECK (
                        length(candidate_sha) = 40 AND
                        candidate_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    expected_remote_sha TEXT NOT NULL CHECK (
                        length(expected_remote_sha) = 40 AND
                        expected_remote_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    stream_fence INTEGER NOT NULL CHECK (stream_fence >= 1),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (intent_id, attempt_number),
                    UNIQUE (
                        id, intent_id, repository_id, target_ref, candidate_sha,
                        stream_fence
                    ),
                    FOREIGN KEY (repository_id, target_ref)
                        REFERENCES work_package_landing_streams(
                            repository_id, target_ref
                        ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        intent_id, repository_id, target_ref, candidate_sha,
                        expected_remote_sha
                    ) REFERENCES work_package_landing_intents (
                        id, repository_id, target_ref, candidate_sha,
                        landing_base_sha
                    ) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_landing_attempts_intent
                    ON work_package_landing_attempts (
                        intent_id, attempt_number, created_at
                    );

                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_attempt_fenced
                BEFORE INSERT ON work_package_landing_attempts
                WHEN NOT EXISTS (
                    SELECT 1 FROM work_package_landing_streams AS stream
                    WHERE stream.repository_id = NEW.repository_id
                      AND stream.target_ref = NEW.target_ref
                      AND stream.lease_owner = NEW.created_by
                      AND stream.lease_fence = NEW.stream_fence
                      AND stream.lease_expires_at > NEW.created_at
                )
                BEGIN
                    SELECT RAISE(ABORT, 'landing attempt requires the current stream fence');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_attempt_immutable
                BEFORE UPDATE ON work_package_landing_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'landing attempts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_attempt_no_delete
                BEFORE DELETE ON work_package_landing_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'landing attempts are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_landing_receipts (
                    id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL UNIQUE,
                    attempt_id TEXT NOT NULL UNIQUE,
                    batch_id TEXT NOT NULL UNIQUE,
                    repository_id TEXT NOT NULL,
                    target_ref TEXT NOT NULL,
                    candidate_sha TEXT NOT NULL CHECK (
                        length(candidate_sha) = 40 AND
                        candidate_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    observed_sha TEXT NOT NULL CHECK (
                        length(observed_sha) = 40 AND
                        observed_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    recovered INTEGER NOT NULL CHECK (recovered IN (0, 1)),
                    recovery TEXT NOT NULL DEFAULT '',
                    attempt_stream_fence INTEGER NOT NULL
                        CHECK (attempt_stream_fence >= 1),
                    recording_stream_fence INTEGER NOT NULL
                        CHECK (recording_stream_fence >= 1),
                    recorded_by TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL UNIQUE CHECK (
                        length(receipt_digest) = 71 AND
                        receipt_digest LIKE 'sha256:%' AND
                        substr(receipt_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    CHECK (
                        (recovered = 0 AND recovery = '' AND observed_sha = candidate_sha)
                        OR (recovered = 1 AND recovery != '')
                    ),
                    FOREIGN KEY (repository_id, target_ref)
                        REFERENCES work_package_landing_streams(
                            repository_id, target_ref
                        ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        intent_id, batch_id, repository_id, target_ref,
                        candidate_sha
                    ) REFERENCES work_package_landing_intents (
                        id, batch_id, repository_id, target_ref, candidate_sha
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        attempt_id, intent_id, repository_id, target_ref,
                        candidate_sha, attempt_stream_fence
                    ) REFERENCES work_package_landing_attempts (
                        id, intent_id, repository_id, target_ref, candidate_sha,
                        stream_fence
                    ) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_landing_receipts_target
                    ON work_package_landing_receipts (
                        repository_id, target_ref, recorded_at, id
                    );

                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_receipt_fenced
                BEFORE INSERT ON work_package_landing_receipts
                WHEN NOT EXISTS (
                    SELECT 1 FROM work_package_integration_batches AS batch
                    JOIN work_package_landing_streams AS stream
                      ON stream.repository_id = NEW.repository_id
                     AND stream.target_ref = NEW.target_ref
                    WHERE batch.id = NEW.batch_id
                      AND batch.repository_id = NEW.repository_id
                      AND batch.target_ref = NEW.target_ref
                      AND batch.candidate_sha = NEW.candidate_sha
                      AND batch.state = 'certified'
                      AND stream.lease_owner = NEW.recorded_by
                      AND stream.lease_fence = NEW.recording_stream_fence
                      AND stream.lease_expires_at > NEW.recorded_at
                )
                BEGIN
                    SELECT RAISE(ABORT, 'landing receipt requires a certified batch and current stream fence');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_receipt_immutable
                BEFORE UPDATE ON work_package_landing_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'landing receipts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_landing_receipt_no_delete
                BEFORE DELETE ON work_package_landing_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'landing receipts are append-only');
                END;

                -- Publication is not product completion until the controller has
                -- consumed the exact landing receipt, released every integration
                -- WIP token, and closed the current graph.  This append-only row is
                -- the atomic commit receipt for that final station; mutable batch
                -- metadata is only a read-model projection of this authority.
                CREATE TABLE IF NOT EXISTS work_package_publication_finalizations (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL UNIQUE,
                    landing_receipt_id TEXT NOT NULL UNIQUE
                        REFERENCES work_package_landing_receipts(id) ON DELETE RESTRICT,
                    package_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
                    epoch INTEGER NOT NULL CHECK (epoch >= 1),
                    repository_id TEXT NOT NULL
                        REFERENCES project_repositories(id) ON DELETE RESTRICT,
                    integration_task_id TEXT NOT NULL
                        REFERENCES tasks(id) ON DELETE RESTRICT,
                    certification_task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
                    certification_id TEXT NOT NULL
                        REFERENCES work_package_certifications(id) ON DELETE RESTRICT,
                    candidate_sha TEXT NOT NULL CHECK (
                        length(candidate_sha) = 40 AND
                        candidate_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    candidate_ref TEXT NOT NULL CHECK (candidate_ref LIKE 'refs/mac/%'),
                    assembly_base_sha TEXT NOT NULL,
                    landing_base_sha TEXT NOT NULL,
                    target_ref TEXT NOT NULL CHECK (target_ref LIKE 'refs/heads/%'),
                    observed_sha TEXT NOT NULL CHECK (
                        length(observed_sha) = 40 AND
                        observed_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    landing_receipt_digest TEXT NOT NULL CHECK (
                        length(landing_receipt_digest) = 71 AND
                        landing_receipt_digest LIKE 'sha256:%' AND
                        substr(landing_receipt_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    released_wip_ids TEXT NOT NULL CHECK (
                        json_valid(released_wip_ids) AND
                        json_type(released_wip_ids) = 'array'
                    ),
                    controller_station_receipt_ids TEXT NOT NULL CHECK (
                        json_valid(controller_station_receipt_ids) AND
                        json_type(controller_station_receipt_ids) = 'array'
                    ),
                    finalization_digest TEXT NOT NULL UNIQUE CHECK (
                        length(finalization_digest) = 71 AND
                        finalization_digest LIKE 'sha256:%' AND
                        substr(finalization_digest, 8) NOT GLOB '*[^0-9a-f]*'
                    ),
                    finalized_by TEXT NOT NULL,
                    finalized_at TEXT NOT NULL,
                    FOREIGN KEY (package_id, epoch, plan_version)
                        REFERENCES work_package_epochs(package_id, epoch, plan_version)
                        ON DELETE RESTRICT,
                    FOREIGN KEY (
                        batch_id, package_id, plan_version, epoch, candidate_sha,
                        assembly_base_sha, landing_base_sha, target_ref
                    ) REFERENCES work_package_integration_batches (
                        id, package_id, plan_version, epoch, candidate_sha,
                        assembly_base_sha, landing_base_sha, target_ref
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (
                        certification_id, batch_id, package_id, plan_version, epoch,
                        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
                    ) REFERENCES work_package_certifications (
                        id, batch_id, package_id, plan_version, epoch,
                        candidate_sha, assembly_base_sha, landing_base_sha, target_ref
                    ) ON DELETE RESTRICT,
                    FOREIGN KEY (batch_id, integration_task_id)
                        REFERENCES work_package_integration_batches(id, integration_task_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_publication_finalizations_package
                    ON work_package_publication_finalizations (
                        package_id, epoch, finalized_at, id
                    );

                CREATE TRIGGER IF NOT EXISTS trg_work_package_publication_finalization_exact
                BEFORE INSERT ON work_package_publication_finalizations
                WHEN NOT EXISTS (
                    SELECT 1
                    FROM work_package_integration_batches AS batch
                    JOIN work_package_landing_receipts AS receipt
                      ON receipt.id = NEW.landing_receipt_id
                     AND receipt.batch_id = batch.id
                    JOIN work_package_landing_intents AS intent
                      ON intent.id = receipt.intent_id
                     AND intent.batch_id = batch.id
                    JOIN work_package_certifications AS certification
                      ON certification.id = NEW.certification_id
                     AND certification.id = intent.certification_id
                     AND certification.batch_id = batch.id
                    JOIN work_packages AS package ON package.id = batch.package_id
                    JOIN work_package_epochs AS epoch
                      ON epoch.package_id = batch.package_id
                     AND epoch.plan_version = batch.plan_version
                     AND epoch.epoch = batch.epoch
                    WHERE batch.id = NEW.batch_id
                      AND batch.package_id = NEW.package_id
                      AND batch.plan_version = NEW.plan_version
                      AND batch.epoch = NEW.epoch
                      AND batch.repository_id = NEW.repository_id
                      AND batch.integration_task_id = NEW.integration_task_id
                      AND batch.candidate_sha = NEW.candidate_sha
                      AND batch.candidate_ref = NEW.candidate_ref
                      AND batch.assembly_base_sha = NEW.assembly_base_sha
                      AND batch.landing_base_sha = NEW.landing_base_sha
                      AND batch.target_ref = NEW.target_ref
                      AND batch.state = 'published'
                      AND receipt.repository_id = NEW.repository_id
                      AND receipt.target_ref = NEW.target_ref
                      AND receipt.candidate_sha = NEW.candidate_sha
                      AND receipt.observed_sha = NEW.observed_sha
                      AND receipt.receipt_digest = NEW.landing_receipt_digest
                      AND certification.package_id = NEW.package_id
                      AND certification.plan_version = NEW.plan_version
                      AND certification.epoch = NEW.epoch
                      AND certification.candidate_sha = NEW.candidate_sha
                      AND certification.assembly_base_sha = NEW.assembly_base_sha
                      AND certification.landing_base_sha = NEW.landing_base_sha
                      AND certification.target_ref = NEW.target_ref
                      AND certification.status IN ('passed', 'published')
                      AND package.current_plan_version = NEW.plan_version
                      AND package.current_epoch = NEW.epoch
                      AND package.state = 'completed'
                      AND epoch.status = 'completed'
                )
                BEGIN
                    SELECT RAISE(ABORT, 'publication finalization identity is not exact');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_publication_finalization_wip
                BEFORE INSERT ON work_package_publication_finalizations
                WHEN json_array_length(NEW.released_wip_ids) = 0
                 OR json_array_length(NEW.released_wip_ids) != (
                    SELECT COUNT(DISTINCT item.value)
                    FROM json_each(NEW.released_wip_ids) AS item
                 )
                 OR EXISTS (
                    SELECT 1 FROM json_each(NEW.released_wip_ids) AS item
                    WHERE NOT EXISTS (
                        SELECT 1 FROM work_package_wip_tokens AS token
                        WHERE token.id = item.value
                          AND token.package_id = NEW.package_id
                          AND token.plan_version = NEW.plan_version
                          AND token.epoch = NEW.epoch
                          AND token.stage = 'integration'
                          AND token.state = 'released'
                          AND token.reservation_key = NEW.batch_id
                          AND token.predecessor_token_id IS NOT NULL
                          AND token.release_reason =
                              'publication_finalized:' || NEW.landing_receipt_id
                    )
                 ) OR EXISTS (
                    SELECT 1 FROM work_package_wip_tokens AS token
                    WHERE token.package_id = NEW.package_id
                      AND token.plan_version = NEW.plan_version
                      AND token.epoch = NEW.epoch
                      AND token.stage = 'integration'
                      AND token.reservation_key = NEW.batch_id
                      AND NOT EXISTS (
                          SELECT 1 FROM json_each(NEW.released_wip_ids) AS item
                          WHERE item.value = token.id
                      )
                 ) OR EXISTS (
                    SELECT 1 FROM work_package_wip_tokens AS token
                    WHERE token.package_id = NEW.package_id
                      AND token.plan_version = NEW.plan_version
                      AND token.epoch = NEW.epoch
                      AND token.state = 'held'
                 )
                BEGIN
                    SELECT RAISE(ABORT, 'publication finalization WIP is incomplete');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_publication_finalization_stations
                BEFORE INSERT ON work_package_publication_finalizations
                WHEN json_array_length(NEW.controller_station_receipt_ids) = 0
                 OR json_array_length(NEW.controller_station_receipt_ids) != (
                    SELECT COUNT(DISTINCT item.value)
                    FROM json_each(NEW.controller_station_receipt_ids) AS item
                 )
                 OR EXISTS (
                    SELECT 1
                    FROM json_each(NEW.controller_station_receipt_ids) AS item
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM work_package_controller_station_receipts AS receipt
                        WHERE receipt.id = item.value
                          AND receipt.batch_id = NEW.batch_id
                          AND receipt.package_id = NEW.package_id
                          AND receipt.plan_version = NEW.plan_version
                          AND receipt.epoch = NEW.epoch
                          AND (
                              (receipt.station_kind = 'integration'
                               AND receipt.outcome = 'integrated'
                               AND receipt.task_id = NEW.integration_task_id) OR
                              (receipt.station_kind = 'certification'
                               AND receipt.outcome = 'certified'
                               AND receipt.task_id = NEW.certification_task_id
                               AND receipt.certification_id = NEW.certification_id)
                          )
                    )
                 ) OR NOT EXISTS (
                    SELECT 1
                    FROM work_package_controller_station_receipts AS receipt
                    JOIN tasks AS task ON task.id = receipt.task_id
                    JOIN work_package_task_links AS link ON link.task_id = receipt.task_id
                    WHERE receipt.batch_id = NEW.batch_id
                      AND receipt.package_id = NEW.package_id
                      AND receipt.plan_version = NEW.plan_version
                      AND receipt.epoch = NEW.epoch
                      AND receipt.station_kind = 'integration'
                      AND receipt.outcome = 'integrated'
                      AND receipt.task_id = NEW.integration_task_id
                      AND task.state = 'completed'
                      AND link.node_state = 'integrated'
                      AND EXISTS (
                          SELECT 1
                          FROM json_each(NEW.controller_station_receipt_ids) AS item
                          WHERE item.value = receipt.id
                      )
                 ) OR (
                    NEW.certification_task_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1
                        FROM work_package_controller_station_receipts AS receipt
                        JOIN tasks AS task ON task.id = receipt.task_id
                        JOIN work_package_task_links AS link
                          ON link.task_id = receipt.task_id
                        WHERE receipt.batch_id = NEW.batch_id
                          AND receipt.package_id = NEW.package_id
                          AND receipt.plan_version = NEW.plan_version
                          AND receipt.epoch = NEW.epoch
                          AND receipt.station_kind = 'certification'
                          AND receipt.outcome = 'certified'
                          AND receipt.task_id = NEW.certification_task_id
                          AND receipt.certification_id = NEW.certification_id
                          AND task.state = 'completed'
                          AND link.node_state = 'certified'
                          AND EXISTS (
                              SELECT 1
                              FROM json_each(
                                  NEW.controller_station_receipt_ids
                              ) AS item
                              WHERE item.value = receipt.id
                          )
                    )
                 ) OR (
                    NEW.certification_task_id IS NULL AND EXISTS (
                        SELECT 1
                        FROM work_package_controller_station_receipts AS receipt
                        WHERE receipt.batch_id = NEW.batch_id
                          AND receipt.station_kind = 'certification'
                          AND receipt.outcome = 'certified'
                    )
                 ) OR EXISTS (
                    SELECT 1
                    FROM work_package_controller_station_receipts AS receipt
                    WHERE receipt.batch_id = NEW.batch_id
                      AND receipt.outcome IN ('integrated', 'certified')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM json_each(NEW.controller_station_receipt_ids) AS item
                          WHERE item.value = receipt.id
                      )
                 )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'publication finalization station provenance is incomplete'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS trg_work_package_publication_finalization_immutable
                BEFORE UPDATE ON work_package_publication_finalizations
                BEGIN
                    SELECT RAISE(ABORT, 'publication finalizations are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_publication_finalization_no_delete
                BEFORE DELETE ON work_package_publication_finalizations
                BEGIN
                    SELECT RAISE(ABORT, 'publication finalizations are append-only');
                END;

                -- Exact, controller-owned authority for asynchronous cleanup
                -- of protected work-package Git refs.  Intents and receipts
                -- are append-only; failed attempts remain durable and do not
                -- prevent a later exact-SHA retry.
                CREATE TABLE IF NOT EXISTS work_package_ref_retirement_intents (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL
                        REFERENCES project_repositories(id) ON DELETE RESTRICT,
                    ref_kind TEXT NOT NULL CHECK (ref_kind IN ('attempt', 'candidate')),
                    ref TEXT NOT NULL,
                    expected_sha TEXT NOT NULL CHECK (
                        length(expected_sha) IN (40, 64) AND
                        expected_sha NOT GLOB '*[^0-9a-f]*'
                    ),
                    task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
                    batch_id TEXT REFERENCES work_package_integration_batches(id)
                        ON DELETE RESTRICT,
                    terminal_state TEXT NOT NULL CHECK (terminal_state != ''),
                    terminal_at TEXT NOT NULL,
                    eligible_after TEXT NOT NULL,
                    created_by TEXT NOT NULL CHECK (created_by != ''),
                    created_at TEXT NOT NULL,
                    UNIQUE (repository_id, ref, expected_sha),
                    CHECK (
                        (ref_kind = 'attempt' AND ref LIKE 'refs/mac/attempts/%'
                         AND task_id IS NOT NULL AND batch_id IS NULL) OR
                        (ref_kind = 'candidate'
                         AND (ref LIKE 'refs/mac/integration/%'
                              OR ref LIKE 'refs/mac/candidates/%')
                         AND task_id IS NULL AND batch_id IS NOT NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_ref_retirement_due
                    ON work_package_ref_retirement_intents (
                        repository_id, eligible_after, ref
                    );
                CREATE TRIGGER IF NOT EXISTS trg_work_package_ref_retirement_intent_immutable
                BEFORE UPDATE ON work_package_ref_retirement_intents
                BEGIN
                    SELECT RAISE(ABORT, 'work-package ref retirement intents are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_ref_retirement_intent_no_delete
                BEFORE DELETE ON work_package_ref_retirement_intents
                BEGIN
                    SELECT RAISE(ABORT, 'work-package ref retirement intents are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_ref_retirement_attempts (
                    id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL
                        REFERENCES work_package_ref_retirement_intents(id)
                        ON DELETE RESTRICT,
                    outcome TEXT NOT NULL CHECK (outcome IN ('failed', 'deleted', 'missing')),
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    CHECK ((outcome = 'failed' AND error != '') OR
                           (outcome != 'failed' AND error = ''))
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_ref_retirement_attempts_intent
                    ON work_package_ref_retirement_attempts (intent_id, created_at);
                CREATE TRIGGER IF NOT EXISTS trg_work_package_ref_retirement_attempt_immutable
                BEFORE UPDATE ON work_package_ref_retirement_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'work-package ref retirement attempts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_ref_retirement_attempt_no_delete
                BEFORE DELETE ON work_package_ref_retirement_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'work-package ref retirement attempts are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_ref_retirement_receipts (
                    id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL UNIQUE
                        REFERENCES work_package_ref_retirement_intents(id)
                        ON DELETE RESTRICT,
                    outcome TEXT NOT NULL CHECK (outcome IN ('deleted', 'missing')),
                    completed_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS trg_work_package_ref_retirement_receipt_immutable
                BEFORE UPDATE ON work_package_ref_retirement_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'work-package ref retirement receipts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_ref_retirement_receipt_no_delete
                BEFORE DELETE ON work_package_ref_retirement_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'work-package ref retirement receipts are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_history (
                    id TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL REFERENCES work_packages(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL CHECK (seq >= 1),
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    plan_version INTEGER,
                    epoch INTEGER,
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE (package_id, seq),
                    CHECK (
                        (plan_version IS NULL AND epoch IS NULL) OR
                        (plan_version IS NOT NULL AND epoch IS NOT NULL)
                    ),
                    FOREIGN KEY (package_id, epoch, plan_version)
                        REFERENCES work_package_epochs(package_id, epoch, plan_version)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_history_package
                    ON work_package_history (package_id, seq);
                CREATE TRIGGER IF NOT EXISTS trg_work_package_history_immutable
                BEFORE UPDATE ON work_package_history
                BEGIN
                    SELECT RAISE(ABORT, 'work package history is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_history_no_delete
                BEFORE DELETE ON work_package_history
                BEGIN
                    SELECT RAISE(ABORT, 'work package history is append-only');
                END;

                -- One-time, append-only data-migration receipts.  Keeping the
                -- marker separate from the assignment rows lets startup prove
                -- a historical scan already completed without scanning the
                -- task and package catalogs again.
                CREATE TABLE IF NOT EXISTS telemetry_data_migrations (
                    version TEXT PRIMARY KEY,
                    component TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    applied_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS trg_telemetry_data_migration_immutable
                BEFORE UPDATE ON telemetry_data_migrations
                BEGIN
                    SELECT RAISE(ABORT, 'telemetry data migrations are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_telemetry_data_migration_no_delete
                BEFORE DELETE ON telemetry_data_migrations
                BEGIN
                    SELECT RAISE(ABORT, 'telemetry data migrations are append-only');
                END;

                CREATE TABLE IF NOT EXISTS execution_cohort_configurations (
                    rollout_revision INTEGER PRIMARY KEY
                        CHECK (rollout_revision >= 1),
                    algorithm TEXT NOT NULL,
                    treatment_percentage INTEGER NOT NULL
                        CHECK (treatment_percentage BETWEEN 0 AND 100),
                    assignment_key_fingerprint TEXT NOT NULL CHECK (
                        length(assignment_key_fingerprint) = 71 AND
                        assignment_key_fingerprint LIKE 'sha256:%' AND
                        substr(assignment_key_fingerprint, 8)
                            NOT GLOB '*[^0-9a-f]*'
                    ),
                    created_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS trg_execution_cohort_configuration_immutable
                BEFORE UPDATE ON execution_cohort_configurations
                BEGIN
                    SELECT RAISE(ABORT, 'execution cohort configurations are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_execution_cohort_configuration_no_delete
                BEFORE DELETE ON execution_cohort_configurations
                BEGIN
                    SELECT RAISE(ABORT, 'execution cohort configurations are append-only');
                END;

                -- Immutable treatment assignment for prospective comparison of
                -- legacy asynchronous tasks with managed synchronized work.
                -- Historical package route is synchronized only when an exact
                -- publication finalization receipt proves the full pipeline;
                -- otherwise its managed execution mode is explicitly unknown.
                CREATE TABLE IF NOT EXISTS execution_cohort_assignments (
                    id TEXT PRIMARY KEY,
                    -- Soft identities are intentional: task/package lifecycle
                    -- cleanup must not erase or block immutable experiment
                    -- assignment history.
                    task_id TEXT UNIQUE,
                    package_id TEXT UNIQUE,
                    eligibility TEXT NOT NULL CHECK (
                        eligibility IN ('eligible', 'ineligible', 'unknown')
                    ),
                    treatment_route TEXT NOT NULL CHECK (
                        treatment_route IN (
                            'legacy_async', 'managed_synchronized',
                            'unknown_managed_mode'
                        )
                    ),
                    rollout_revision INTEGER NOT NULL CHECK (rollout_revision >= 0),
                    cohort_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    assigned_by TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    CHECK (task_id IS NOT NULL OR package_id IS NOT NULL)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_cohort_route
                    ON execution_cohort_assignments (
                        treatment_route, eligibility, assigned_at, id
                    );
                CREATE TRIGGER IF NOT EXISTS trg_execution_cohort_immutable
                BEFORE UPDATE ON execution_cohort_assignments
                BEGIN
                    SELECT RAISE(ABORT, 'execution cohort assignments are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_execution_cohort_no_delete
                BEFORE DELETE ON execution_cohort_assignments
                BEGIN
                    SELECT RAISE(ABORT, 'execution cohort assignments are append-only');
                END;

                CREATE TABLE IF NOT EXISTS work_package_station_attempts (
                    id TEXT PRIMARY KEY,
                    assignment_id TEXT NOT NULL
                        REFERENCES execution_cohort_assignments(id) ON DELETE RESTRICT,
                    package_id TEXT NOT NULL
                        REFERENCES work_packages(id) ON DELETE RESTRICT,
                    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
                    epoch INTEGER NOT NULL CHECK (epoch >= 1),
                    station TEXT NOT NULL CHECK (station IN (
                        'controller', 'admission', 'integration', 'certification',
                        'landing', 'finalization'
                    )),
                    operation TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                    attempted INTEGER NOT NULL CHECK (attempted IN (0, 1)),
                    pipeline_run_id TEXT NOT NULL DEFAULT '',
                    outcome_index INTEGER NOT NULL DEFAULT 0 CHECK (outcome_index >= 0),
                    batch_id TEXT NOT NULL DEFAULT '',
                    job_id TEXT NOT NULL DEFAULT '',
                    queued_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    queue_duration_ms INTEGER NOT NULL CHECK (queue_duration_ms >= 0),
                    execution_duration_ms INTEGER NOT NULL
                        CHECK (execution_duration_ms >= 0),
                    terminal_status TEXT NOT NULL CHECK (terminal_status IN (
                        'succeeded', 'failed', 'busy', 'held', 'stale',
                        'rejected', 'skipped'
                    )),
                    reason_code TEXT NOT NULL DEFAULT '',
                    failure_class TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    recorded_at TEXT NOT NULL,
                    UNIQUE (package_id, station, attempt_number),
                    UNIQUE (pipeline_run_id, outcome_index),
                    FOREIGN KEY (package_id, epoch, plan_version)
                        REFERENCES work_package_epochs(package_id, epoch, plan_version)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_station_attempts_package
                    ON work_package_station_attempts (
                        package_id, station, completed_at, id
                    );
                CREATE INDEX IF NOT EXISTS idx_work_package_station_attempts_status
                    ON work_package_station_attempts (
                        terminal_status, failure_class, completed_at, id
                    );
                CREATE TRIGGER IF NOT EXISTS trg_work_package_station_attempt_immutable
                BEFORE UPDATE ON work_package_station_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'work-package station attempts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_station_attempt_no_delete
                BEFORE DELETE ON work_package_station_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'work-package station attempts are append-only');
                END;

                -- Lossless controller projection.  Unlike normalized station
                -- attempts it permits package-less inventory/run outcomes and
                -- retains unknown future operations explicitly.
                CREATE TABLE IF NOT EXISTS work_package_controller_outcomes (
                    id TEXT PRIMARY KEY,
                    pipeline_run_id TEXT NOT NULL,
                    outcome_index INTEGER NOT NULL CHECK (outcome_index >= -1),
                    package_id TEXT NOT NULL DEFAULT '',
                    plan_version INTEGER NOT NULL CHECK (plan_version >= 0),
                    epoch INTEGER NOT NULL CHECK (epoch >= 0),
                    operation TEXT NOT NULL,
                    attempted INTEGER NOT NULL CHECK (attempted IN (0, 1)),
                    batch_id TEXT NOT NULL DEFAULT '',
                    job_id TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    execution_duration_ms INTEGER NOT NULL
                        CHECK (execution_duration_ms >= 0),
                    status TEXT NOT NULL,
                    terminal_status TEXT NOT NULL CHECK (terminal_status IN (
                        'succeeded', 'failed', 'busy', 'held', 'stale',
                        'rejected', 'skipped'
                    )),
                    reason_code TEXT NOT NULL DEFAULT '',
                    failure_class TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '{}',
                    recorded_at TEXT NOT NULL,
                    UNIQUE (pipeline_run_id, outcome_index)
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_controller_outcomes_package
                    ON work_package_controller_outcomes (
                        package_id, completed_at, id
                    );
                CREATE INDEX IF NOT EXISTS idx_work_package_controller_outcomes_status
                    ON work_package_controller_outcomes (
                        terminal_status, failure_class, completed_at, id
                    );
                CREATE TRIGGER IF NOT EXISTS trg_work_package_controller_outcome_immutable
                BEFORE UPDATE ON work_package_controller_outcomes
                BEGIN
                    SELECT RAISE(ABORT, 'work-package controller outcomes are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_controller_outcome_no_delete
                BEFORE DELETE ON work_package_controller_outcomes
                BEGIN
                    SELECT RAISE(ABORT, 'work-package controller outcomes are append-only');
                END;

                -- This health record is intentionally outside the telemetry
                -- writer path.  The observer increments it only after a
                -- measurement failure, avoiding recursive telemetry.
                CREATE TABLE IF NOT EXISTS work_package_telemetry_health (
                    singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'pipeline'),
                    failure_count INTEGER NOT NULL DEFAULT 0
                        CHECK (failure_count >= 0),
                    last_failure_operation TEXT NOT NULL DEFAULT '',
                    last_error_type TEXT NOT NULL DEFAULT '',
                    last_error_fingerprint TEXT NOT NULL DEFAULT '',
                    last_failed_at TEXT,
                    last_success_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS work_package_finalization_outcomes (
                    id TEXT PRIMARY KEY,
                    finalization_id TEXT NOT NULL
                        REFERENCES work_package_publication_finalizations(id)
                        ON DELETE RESTRICT,
                    package_id TEXT NOT NULL
                        REFERENCES work_packages(id) ON DELETE RESTRICT,
                    outcome_type TEXT NOT NULL CHECK (
                        outcome_type IN ('revert', 'incident')
                    ),
                    external_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE (finalization_id, outcome_type, external_id)
                );
                CREATE INDEX IF NOT EXISTS idx_work_package_finalization_outcomes_package
                    ON work_package_finalization_outcomes (
                        package_id, outcome_type, observed_at, id
                    );
                CREATE TRIGGER IF NOT EXISTS trg_work_package_finalization_outcome_immutable
                BEFORE UPDATE ON work_package_finalization_outcomes
                BEGIN
                    SELECT RAISE(ABORT, 'work-package finalization outcomes are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS trg_work_package_finalization_outcome_no_delete
                BEFORE DELETE ON work_package_finalization_outcomes
                BEGIN
                    SELECT RAISE(ABORT, 'work-package finalization outcomes are append-only');
                END;

                -- The two catalog scans and their marker commit atomically.  A
                -- marker-first CROSS JOIN makes the left side empty after v2
                -- has run, so later startups do not visit packages or tasks.
                BEGIN IMMEDIATE;
                INSERT INTO execution_cohort_assignments (
                    id, task_id, package_id, eligibility, treatment_route,
                    rollout_revision, cohort_key, reason, detail, assigned_by,
                    assigned_at
                )
                SELECT
                    'cohort_hist_managed_' || package.id,
                    NULL,
                    package.id,
                    'unknown',
                    CASE WHEN EXISTS (
                        SELECT 1
                        FROM work_package_publication_finalizations AS finalization
                        WHERE finalization.package_id = package.id
                    ) THEN 'managed_synchronized'
                      ELSE 'unknown_managed_mode'
                    END,
                    COALESCE((
                        SELECT revision FROM managed_task_publication_rollout
                        WHERE singleton_key = 'fleet'
                    ), 0),
                    CASE WHEN EXISTS (
                        SELECT 1
                        FROM work_package_publication_finalizations AS finalization
                        WHERE finalization.package_id = package.id
                    ) THEN 'managed_receipted_pre_instrumentation'
                      ELSE 'managed_mode_unknown_pre_instrumentation'
                    END,
                    CASE WHEN EXISTS (
                        SELECT 1
                        FROM work_package_publication_finalizations AS finalization
                        WHERE finalization.package_id = package.id
                    ) THEN 'historical_synchronized_pipeline_receipt'
                      ELSE 'historical_package_mode_unproven'
                    END,
                    json_object(
                        'schema', 'mac.execution_cohort.backfill.v2',
                        'eligibility_source', 'unavailable',
                        'route_source', CASE WHEN EXISTS (
                            SELECT 1
                            FROM work_package_publication_finalizations AS finalization
                            WHERE finalization.package_id = package.id
                        ) THEN 'publication_finalization_receipt'
                          ELSE 'unavailable'
                        END,
                        'route_receipt_id', COALESCE((
                            SELECT finalization.id
                            FROM work_package_publication_finalizations AS finalization
                            WHERE finalization.package_id = package.id
                            ORDER BY finalization.finalized_at, finalization.id
                            LIMIT 1
                        ), '')
                    ),
                    'schema-migration',
                    package.created_at
                FROM (
                    SELECT 1 AS run
                    WHERE NOT EXISTS (
                        SELECT 1 FROM telemetry_data_migrations
                        WHERE version = 'execution_cohort_historical_backfill_v2'
                    )
                ) AS migration_needed
                CROSS JOIN work_packages AS package
                WHERE NOT EXISTS (
                    SELECT 1 FROM execution_cohort_assignments AS assignment
                    WHERE assignment.package_id = package.id
                );

                INSERT INTO execution_cohort_assignments (
                    id, task_id, package_id, eligibility, treatment_route,
                    rollout_revision, cohort_key, reason, detail, assigned_by,
                    assigned_at
                )
                SELECT
                    'cohort_hist_legacy_' || task.id,
                    task.id,
                    NULL,
                    'unknown',
                    'legacy_async',
                    0,
                    CASE
                        WHEN json_extract(
                            task.metadata, '$.managed_fast_lane.activation'
                        ) = 'legacy_compatibility'
                        THEN 'legacy_atomic_shape_pre_instrumentation'
                        ELSE 'legacy_pre_instrumentation_unknown'
                    END,
                    CASE
                        WHEN json_extract(
                            task.metadata, '$.managed_fast_lane.activation'
                        ) = 'legacy_compatibility'
                        THEN 'historical_control_plane_route_projection'
                        ELSE 'historical_absence_of_package_linkage'
                    END,
                    json_object(
                        'schema', 'mac.execution_cohort.backfill.v2',
                        'eligibility_source', 'unavailable',
                        'shape_eligibility_source', CASE
                            WHEN json_extract(
                                task.metadata, '$.managed_fast_lane.activation'
                            ) = 'legacy_compatibility'
                            THEN 'control_plane_managed_fast_lane_projection'
                            ELSE 'unavailable'
                        END,
                        'route_source', 'absence_of_work_package_link'
                    ),
                    'schema-migration',
                    task.created_at
                FROM (
                    SELECT 1 AS run
                    WHERE NOT EXISTS (
                        SELECT 1 FROM telemetry_data_migrations
                        WHERE version = 'execution_cohort_historical_backfill_v2'
                    )
                ) AS migration_needed
                CROSS JOIN tasks AS task
                WHERE NOT EXISTS (
                    SELECT 1 FROM work_package_task_links AS link
                    WHERE link.task_id = task.id
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM work_packages AS package
                    WHERE package.root_task_id = task.id
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM execution_cohort_assignments AS assignment
                    WHERE assignment.task_id = task.id
                );
                INSERT OR IGNORE INTO telemetry_data_migrations (
                    version, component, detail, applied_at
                ) VALUES (
                    'execution_cohort_historical_backfill_v2',
                    'execution_cohort_assignments',
                    '{"schema":"mac.telemetry_data_migration.v1","historical_backfill":"mac.execution_cohort.backfill.v2"}',
                    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                );
                COMMIT;

                -- Provisioning requests: durable record of "the swarm needs
                -- an agent it does not have." Surfaced by the dispatcher
                -- and the default-review workflow when no eligible agent
                -- can be selected. A future provisioner (k8s operator,
                -- nomad job, local spawner) polls this table.
                CREATE TABLE IF NOT EXISTS agent_provisioning_requests (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    role_slug TEXT,
                    capabilities TEXT NOT NULL DEFAULT '[]',
                    hardware TEXT NOT NULL DEFAULT '{}',
                    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
                    detail TEXT NOT NULL DEFAULT '{}',
                    fulfilled_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
                    requested_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_provisioning_status
                    ON agent_provisioning_requests (status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_agent_provisioning_role
                    ON agent_provisioning_requests (role_slug, status);

                -- Unified audit stream. Operators query one surface instead of
                -- joining four per-resource tables. The view is read-only; each
                -- write still goes to its owning table inside the originating
                -- transaction, so audit trail and durable state commit together.
                DROP VIEW IF EXISTS events;
                CREATE VIEW events AS
                    SELECT
                        id,
                        'task' AS subject_type,
                        task_id AS subject_id,
                        event_type,
                        actor,
                        json_set(
                            COALESCE(NULLIF(detail, ''), '{}'),
                            '$.from_state', from_state,
                            '$.to_state', to_state
                        ) AS detail,
                        created_at
                    FROM task_history
                    UNION ALL
                    SELECT id, 'rollout', rollout_id, event_type, actor, detail, created_at
                    FROM rollout_events
                    UNION ALL
                    SELECT id, 'eval_set', eval_set_id, event_type, actor, detail, created_at
                    FROM eval_set_events
                    UNION ALL
                    SELECT
                        id,
                        'secret',
                        secret_id,
                        'secret.' || result,
                        accessor_agent_id,
                        json_object(
                            'purpose', purpose,
                            'expires_at', expires_at,
                            'revealed_at', revealed_at
                        ),
                        created_at
                    FROM secret_access_audit
                    UNION ALL
                    SELECT id, 'environment', environment_id, event_type, actor, detail, created_at
                    FROM environment_events
                    UNION ALL
                    SELECT id, 'project', project_id, event_type, actor, detail, created_at
                    FROM project_events
                    UNION ALL
                    SELECT id, 'fleet', fleet_id, event_type, actor, detail, created_at
                    FROM fleet_events
                    UNION ALL
                    SELECT id, 'agent', agent_id, event_type, actor, detail, created_at
                    FROM agent_lifecycle_events
                    UNION ALL
                    SELECT id, 'agent', agent_id, event_type, actor, detail, created_at
                    FROM agent_events
                    UNION ALL
                    SELECT
                        id,
                        'work_package',
                        package_id,
                        event_type,
                        actor,
                        json_set(
                            COALESCE(NULLIF(detail, ''), '{}'),
                            '$.plan_version', plan_version,
                            '$.epoch', epoch
                        ),
                        created_at
                    FROM work_package_history
                    UNION ALL
                    SELECT
                        id,
                        CASE WHEN package_id IS NULL THEN 'task' ELSE 'work_package' END,
                        COALESCE(package_id, task_id),
                        'execution.cohort_assigned',
                        assigned_by,
                        json_set(
                            COALESCE(NULLIF(detail, ''), '{}'),
                            '$.task_id', task_id,
                            '$.package_id', package_id,
                            '$.eligibility', eligibility,
                            '$.treatment_route', treatment_route,
                            '$.rollout_revision', rollout_revision,
                            '$.cohort_key', cohort_key,
                            '$.reason', reason
                        ),
                        assigned_at
                    FROM execution_cohort_assignments
                    UNION ALL
                    SELECT
                        id,
                        'work_package',
                        package_id,
                        'work_package.station.' || station || '.' || terminal_status,
                        actor,
                        json_set(
                            COALESCE(NULLIF(detail, ''), '{}'),
                            '$.assignment_id', assignment_id,
                            '$.plan_version', plan_version,
                            '$.epoch', epoch,
                            '$.station', station,
                            '$.operation', operation,
                            '$.attempt_number', attempt_number,
                            '$.attempted', json(CASE WHEN attempted = 1 THEN 'true' ELSE 'false' END),
                            '$.pipeline_run_id', pipeline_run_id,
                            '$.outcome_index', outcome_index,
                            '$.batch_id', batch_id,
                            '$.job_id', job_id,
                            '$.queued_at', queued_at,
                            '$.started_at', started_at,
                            '$.completed_at', completed_at,
                            '$.queue_duration_ms', queue_duration_ms,
                            '$.execution_duration_ms', execution_duration_ms,
                            '$.terminal_status', terminal_status,
                            '$.reason_code', reason_code,
                            '$.failure_class', failure_class
                        ),
                        completed_at
                    FROM work_package_station_attempts
                    UNION ALL
                    SELECT
                        id,
                        CASE WHEN package_id = '' THEN 'service' ELSE 'work_package' END,
                        CASE WHEN package_id = '' THEN 'work-package-pipeline' ELSE package_id END,
                        'work_package.controller.' || operation || '.' || terminal_status,
                        'work-package-pipeline',
                        json_set(
                            COALESCE(NULLIF(detail, ''), '{}'),
                            '$.pipeline_run_id', pipeline_run_id,
                            '$.outcome_index', outcome_index,
                            '$.plan_version', plan_version,
                            '$.epoch', epoch,
                            '$.operation', operation,
                            '$.attempted', json(CASE WHEN attempted = 1 THEN 'true' ELSE 'false' END),
                            '$.batch_id', batch_id,
                            '$.job_id', job_id,
                            '$.started_at', started_at,
                            '$.completed_at', completed_at,
                            '$.execution_duration_ms', execution_duration_ms,
                            '$.status', status,
                            '$.terminal_status', terminal_status,
                            '$.reason_code', reason_code,
                            '$.failure_class', failure_class
                        ),
                        completed_at
                    FROM work_package_controller_outcomes
                    UNION ALL
                    SELECT
                        id,
                        'work_package',
                        package_id,
                        'work_package.finalization.' || outcome_type,
                        actor,
                        json_set(
                            COALESCE(NULLIF(detail, ''), '{}'),
                            '$.finalization_id', finalization_id,
                            '$.outcome_type', outcome_type,
                            '$.external_id', external_id,
                            '$.observed_at', observed_at
                        ),
                        observed_at
                    FROM work_package_finalization_outcomes
                    UNION ALL
                    SELECT
                        id,
                        CASE WHEN task_id IS NOT NULL THEN 'task' ELSE 'agent' END,
                        COALESCE(task_id, agent_id),
                        'command.' || phase,
                        agent_id,
                        json_object(
                            'command_id', command_id,
                            'agent_id', agent_id,
                            'argv0', json_extract(argv, '$[0]'),
                            'argv_redacted', json('true'),
                            'cwd', cwd,
                            'task_id', task_id,
                            'lease_id', lease_id,
                            'started_at', started_at,
                            'completed_at', completed_at,
                            'duration_ms', duration_ms,
                            'returncode', returncode,
                            'stdout_sha256', stdout_sha256,
                            'stderr_sha256', stderr_sha256,
                            'stdout_bytes', stdout_bytes,
                            'stderr_bytes', stderr_bytes,
                            'metadata', json(metadata)
                        ),
                        created_at
                    FROM command_audit
                    UNION ALL
                    SELECT
                        event_id,
                        COALESCE(NULLIF(subject_type, ''), 'action_event'),
                        COALESCE(subject_id, event_id),
                        'action.' || action_type || '.' || action_name,
                        actor,
                        json_object(
                            'schema', 'mac.action_event.v1',
                            'agent_id', agent_id,
                            'hermes_instance_id', hermes_instance_id,
                            'task_id', task_id,
                            'session_id', session_id,
                            'sandbox_id', sandbox_id,
                            'action_type', action_type,
                            'action_name', action_name,
                            'outcome', outcome,
                            'severity', severity,
                            'policy_id', policy_id,
                            'policy_version', policy_version,
                            'command_id', command_id,
                            'parent_event_id', parent_event_id,
                            'redaction_state', redaction_state,
                            'attributes', json(attributes)
                        ),
                        timestamp
                    FROM action_events
                    UNION ALL
                    -- Conversation threads project as one event per row: the
                    -- "thread_tracked" observation at last_seen_at. This
                    -- surfaces gateway activity in the unified audit stream
                    -- without needing a sibling events table.
                    SELECT
                        id,
                        'conversation_thread',
                        id,
                        'gateway.thread_tracked',
                        'gateway',
                        json_object(
                            'platform_binding_id', platform_binding_id,
                            'external_thread_id', external_thread_id,
                            'latest_task_id', latest_task_id,
                            'summary', summary
                        ),
                        last_seen_at
                    FROM conversation_threads
                    UNION ALL
                    -- Vector refs project as one event per row: the
                    -- "indexed" observation at creation time.
                    SELECT
                        id,
                        'vector_ref',
                        memory_id,
                        'vector.indexed',
                        created_by,
                        json_object(
                            'vector_db', vector_db,
                            'collection', collection,
                            'point_id', point_id,
                            'embedding_model', embedding_model
                        ),
                        created_at
                    FROM vector_refs;

                -- ----------------------------------------------------------------
                -- Source release registry (mac.source_release.v1)
                -- Immutable record of a reviewed/published commit. Uniqueness on
                -- (repository_id, commit_sha) is enforced by a UNIQUE index.
                -- ----------------------------------------------------------------
                CREATE TABLE IF NOT EXISTS source_releases (
                    id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    canonical_remote_url TEXT NOT NULL,
                    -- Immutable 40-char hex SHA. A CHECK + trigger pair ensures the
                    -- column is never updated after creation.
                    commit_sha TEXT NOT NULL,
                    canonical_ref TEXT NOT NULL,
                    tree_digest TEXT NOT NULL,
                    artifact_digest TEXT,
                    image_digest TEXT,
                    -- Creation provenance
                    created_by TEXT NOT NULL,
                    created_by_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    -- Evidence cross-references
                    review_evidence_id TEXT REFERENCES evidence(id) ON DELETE SET NULL,
                    publication_evidence_id TEXT REFERENCES evidence(id) ON DELETE SET NULL,
                    -- Status lifecycle: draft | reviewed | published | retracted
                    status TEXT NOT NULL DEFAULT 'draft',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    -- One canonical release per (repository, sha)
                    UNIQUE(repository_id, commit_sha),
                    -- Enforce 40-char hex SHA format
                    CHECK(length(commit_sha) = 40 AND commit_sha GLOB '[0-9a-f]*'),
                    -- Reject branch refs
                    CHECK(canonical_ref NOT LIKE 'refs/heads/%')
                );
                CREATE INDEX IF NOT EXISTS idx_source_releases_repo_status
                    ON source_releases (repository_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_source_releases_status_created
                    ON source_releases (status, created_at);
                -- Immutability trigger: once a row exists, commit_sha may never change.
                CREATE TRIGGER IF NOT EXISTS trg_source_releases_sha_immutable
                BEFORE UPDATE OF commit_sha ON source_releases
                FOR EACH ROW
                WHEN NEW.commit_sha != OLD.commit_sha
                BEGIN
                    SELECT RAISE(ABORT, 'source_releases.commit_sha is immutable');
                END;

                -- ----------------------------------------------------------------
                -- Fleet desired-source state (mac.fleet_desired_source.v1)
                -- Current desired release for a fleet/environment scope.
                -- One active row per scope; generation is monotonically increasing.
                -- ----------------------------------------------------------------
                CREATE TABLE IF NOT EXISTS fleet_desired_source_states (
                    id TEXT PRIMARY KEY,
                    -- Scope: exactly one of fleet_id / environment_id must be non-NULL
                    fleet_id TEXT REFERENCES fleets(id) ON DELETE CASCADE,
                    environment_id TEXT REFERENCES environments(id) ON DELETE CASCADE,
                    -- Monotonic generation (>= 1)
                    generation INTEGER NOT NULL,
                    release_id TEXT NOT NULL REFERENCES source_releases(id),
                    rollout_policy TEXT NOT NULL DEFAULT 'immediate',
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    prior_generation INTEGER,
                    paused INTEGER NOT NULL DEFAULT 0,
                    request_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    -- Generation must be positive
                    CHECK(generation >= 1),
                    -- Scope exclusivity: at least one scope column must be set
                    CHECK(fleet_id IS NOT NULL OR environment_id IS NOT NULL)
                );
                -- Partial unique indexes: one desired-source row per fleet/env scope.
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_desired_source_fleet
                    ON fleet_desired_source_states (fleet_id) WHERE fleet_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_desired_source_env
                    ON fleet_desired_source_states (environment_id) WHERE environment_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_fleet
                    ON fleet_desired_source_states (fleet_id, generation);
                CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_env
                    ON fleet_desired_source_states (environment_id, generation);
                -- Monotonicity trigger: generation may only increase.
                CREATE TRIGGER IF NOT EXISTS trg_fleet_desired_source_gen_monotonic
                BEFORE UPDATE OF generation ON fleet_desired_source_states
                FOR EACH ROW
                WHEN NEW.generation <= OLD.generation
                BEGIN
                    SELECT RAISE(ABORT, 'fleet_desired_source_states.generation must increase monotonically');
                END;

                -- ----------------------------------------------------------------
                -- Desired-source transition history (append-only audit log)
                -- ----------------------------------------------------------------
                CREATE TABLE IF NOT EXISTS fleet_desired_source_transitions (
                    id TEXT PRIMARY KEY,
                    desired_source_state_id TEXT NOT NULL
                        REFERENCES fleet_desired_source_states(id) ON DELETE CASCADE,
                    from_generation INTEGER,
                    to_generation INTEGER NOT NULL,
                    release_id TEXT NOT NULL REFERENCES source_releases(id),
                    rollout_policy TEXT NOT NULL DEFAULT 'immediate',
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    request_id TEXT,
                    created_at TEXT NOT NULL,
                    CHECK(to_generation >= 1)
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_transitions_state
                    ON fleet_desired_source_transitions (desired_source_state_id, to_generation);
                CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_transitions_release
                    ON fleet_desired_source_transitions (release_id, created_at);

                -- ----------------------------------------------------------------
                -- Desired-source idempotency records
                -- Prevents double-application of the same request_id per scope.
                -- ----------------------------------------------------------------
                CREATE TABLE IF NOT EXISTS fleet_desired_source_idempotency (
                    id TEXT PRIMARY KEY,
                    -- Denormalised scope key for fast lookup (fleet:<id> | env:<id>)
                    scope_key TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    desired_source_state_id TEXT NOT NULL
                        REFERENCES fleet_desired_source_states(id) ON DELETE CASCADE,
                    generation INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(scope_key, request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_idempotency_scope
                    ON fleet_desired_source_idempotency (scope_key, request_id);

                -- ----------------------------------------------------------------
                -- Evidence-reuse decision audit records
                -- Durable trail of prior-executor-evidence reuse decisions made
                -- when review infrastructure fails (see evidence_reuse_verifier).
                -- ----------------------------------------------------------------
                CREATE TABLE IF NOT EXISTS evidence_reuse_records (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    source_evidence_id TEXT NOT NULL,
                    remote_url TEXT,
                    expected_head_sha TEXT,
                    reused INTEGER NOT NULL,
                    verification TEXT NOT NULL,
                    problems TEXT NOT NULL,
                    decided_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reused_by_agent_id TEXT NOT NULL DEFAULT '',
                    reuse_context TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_reuse_records_task
                    ON evidence_reuse_records (task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_reuse_records_source
                    ON evidence_reuse_records (source_evidence_id, created_at);

                CREATE TABLE IF NOT EXISTS source_convergence_nodes (
                    id TEXT PRIMARY KEY,
                    desired_source_state_id TEXT NOT NULL
                        REFERENCES fleet_desired_source_states(id) ON DELETE CASCADE,
                    fleet_id TEXT NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    desired_generation INTEGER NOT NULL,
                    release_id TEXT NOT NULL REFERENCES source_releases(id),
                    desired_sha TEXT NOT NULL,
                    actual_sha TEXT,
                    action TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    request_id TEXT,
                    stream_id TEXT REFERENCES agentbus_streams(id) ON DELETE SET NULL,
                    next_retry_at TEXT,
                    blocker_code TEXT,
                    blocker_detail TEXT,
                    result TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(fleet_id, agent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_source_convergence_nodes_phase
                    ON source_convergence_nodes (fleet_id, desired_generation, phase);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_source_convergence_nodes_request
                    ON source_convergence_nodes (request_id) WHERE request_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS source_convergence_controller_leases (
                    scope_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                -- Human principals registry: first-class assignable human identities
                -- (username / email / GitHub login) and explicit group membership.
                CREATE TABLE IF NOT EXISTS humans (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT,
                    github_login TEXT UNIQUE,
                    display_name TEXT,
                    groups TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_humans_username
                    ON humans (username);
                CREATE INDEX IF NOT EXISTS idx_humans_github_login
                    ON humans (github_login)
                    WHERE github_login IS NOT NULL;

                CREATE TABLE IF NOT EXISTS human_groups (
                    id TEXT PRIMARY KEY,
                    human_id TEXT NOT NULL REFERENCES humans(id) ON DELETE CASCADE,
                    group_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(human_id, group_name)
                );
                CREATE INDEX IF NOT EXISTS idx_human_groups_human
                    ON human_groups (human_id);
                CREATE INDEX IF NOT EXISTS idx_human_groups_group
                    ON human_groups (group_name);
                """
            )
            if fresh_persona_identity:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._verify_persona_instance_identity()
                    self._record_persona_instance_identity_receipt(
                        version="mac.persona_instance_identity.v1",
                        origin="fresh-schema",
                    )
                    self._conn.execute("COMMIT")
                except BaseException:
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise
            elif self._persona_instance_identity_violations():
                raise StoreError(
                    "persona-instance schema became invalid during initialization: %s"
                    % "; ".join(self._persona_instance_identity_violations())
                )
            self._repair_preliminary_package_cohorts()
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        # Dependency waits are intentionally distinct from actionable blocks.
        # Recreate these triggers because IF NOT EXISTS cannot update the enum
        # on databases created before the WAITING state was introduced.
        self._conn.executescript(
            """
            DROP TRIGGER IF EXISTS trg_tasks_state_enum_ins;
            DROP TRIGGER IF EXISTS trg_tasks_state_enum_upd;
            CREATE TRIGGER trg_tasks_state_enum_ins
            BEFORE INSERT ON tasks
            FOR EACH ROW
            WHEN NEW.state NOT IN (
                'open', 'waiting', 'blocked', 'claimed', 'running',
                'needs_review', 'reviewing', 'completed', 'failed', 'cancelled'
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid task state');
            END;
            CREATE TRIGGER trg_tasks_state_enum_upd
            BEFORE UPDATE OF state ON tasks
            FOR EACH ROW
            WHEN NEW.state NOT IN (
                'open', 'waiting', 'blocked', 'claimed', 'running',
                'needs_review', 'reviewing', 'completed', 'failed', 'cancelled'
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid task state');
            END;
            """
        )
        # beads→mac: the project repository registry was historically the
        # `beads_repositories` table. `project_repositories` is created empty
        # during table setup, so copy any legacy rows over and drop the old
        # table. Idempotent (skipped once the legacy table is gone); columns
        # are identical, so a positional copy is safe.
        legacy_repo_table = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = 'beads_repositories'"
        ).fetchone()
        if legacy_repo_table is not None:
            self._conn.execute(
                "INSERT OR IGNORE INTO project_repositories SELECT * FROM beads_repositories"
            )
            self._conn.execute("DROP TABLE beads_repositories")
        # mac-s2vz: record when an agent's attestation key was last
        # rotated so the verifier can produce a clear error message
        # (key-rotation-after-signature, not "signature does not verify")
        # for evidence signed under a now-retired key.
        self._ensure_column(
            "agents", "attestation_key_rotated_at", "attestation_key_rotated_at TEXT"
        )
        # mac-s2vz followup: retain the immediately-previous attestation key so a
        # routine rotation (e.g. a re-keyed agent after a redeploy) does not
        # permanently invalidate in-flight verdicts signed under the prior key.
        # The verifier checks a signature against the key that was active at
        # signing time (evidence.created_at <= rotated_at -> try the prev key).
        self._ensure_column(
            "agents",
            "attestation_key_prev_ciphertext",
            "attestation_key_prev_ciphertext TEXT",
        )
        self._ensure_column(
            "fleet_release_epoch_agents",
            "prior_report_executor_projection_sha256",
            "prior_report_executor_projection_sha256 TEXT",
        )
        self._ensure_column(
            "fleet_release_epochs",
            "abort_disposition",
            "abort_disposition TEXT",
        )
        # mac-1oi4: capture who asked for an agent so fulfill can refuse
        # a self-fulfill (the same actor approving its own request).
        self._ensure_column(
            "agent_provisioning_requests", "requested_by", "requested_by TEXT"
        )
        self._ensure_column("secret_access_audit", "expires_at", "expires_at TEXT")
        self._ensure_column("secret_access_audit", "revealed_at", "revealed_at TEXT")
        self._ensure_column("publications", "content_hash", "content_hash TEXT")
        self._ensure_column("rollouts", "tenant_id", "tenant_id TEXT")
        self._ensure_column("rollouts", "channel", "channel TEXT NOT NULL DEFAULT 'fleet'")
        self._ensure_column("rollouts", "runtime_environment_id", "runtime_environment_id TEXT")
        self._ensure_column("rollouts", "artifact_uri", "artifact_uri TEXT")
        self._ensure_column("rollouts", "artifact_hash", "artifact_hash TEXT")
        self._ensure_column("rollouts", "health_policy", "health_policy TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("rollouts", "required_eval_set_id", "required_eval_set_id TEXT")
        self._ensure_column("rollouts", "deploy_environment_id", "deploy_environment_id TEXT")
        self._ensure_column("agents", "running_digest", "running_digest TEXT")
        self._ensure_column("agents", "role_id", "role_id TEXT")
        self._ensure_column("agents", "hermes_instance_id", "hermes_instance_id TEXT")
        self._ensure_column(
            "agents",
            "instance_kind",
            "instance_kind TEXT NOT NULL DEFAULT 'static' "
            "CHECK (instance_kind IN ('static', 'fungible'))",
        )
        # task_c394685a: tombstone column — decommissioned agents keep their
        # row so AgentBus streams/events/deliveries survive with real
        # identities instead of cascading away with the agent.
        self._ensure_column("agents", "deleted_at", "deleted_at TEXT")
        # Partial index so list_agents (WHERE deleted_at IS NULL ORDER BY
        # name, id) is an index-only scan over the handful of LIVE agents.
        # Decommissioned/ephemeral agents are tombstoned, never purged, so the
        # agents table grows without bound; without this index the query full-
        # scans every tombstone + filesorts — ~1s for 8 live agents (the
        # /agents latency bug). The partial predicate matches the query's WHERE
        # exactly and stays tiny regardless of tombstone count. Emitted here
        # (not in the CREATE TABLE DDL) because deleted_at is a migration column.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agents_live_name "
            "ON agents (name, id) WHERE deleted_at IS NULL"
        )
        # task_588b67fd: group streams — JSON member list; NULL keeps the
        # legacy sender/recipient pair semantics.
        self._ensure_column("agentbus_streams", "participants", "participants TEXT")
        # task_0d50e190: hub-durable consumer read positions. The position
        # document is opaque to the hub (client-defined bookmark, e.g. an
        # updated_at watermark + per-stream chunk sequences) so gateway
        # rebuilds no longer lose their place.
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agentbus_consumer_cursors (
                agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                topic TEXT NOT NULL,
                position TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (agent_id, topic)
            );
            """
        )
        # task_repair_d771f872: durable resume cursors for the bounded
        # work-package pipeline controller and the repository ref reconciler.
        # Both keep a restart-losable scan bookmark / last-report in process
        # memory; persisting an opaque, bounded document under a stable
        # (scope, name) key lets a hub restart resume where it left off instead
        # of rescanning the whole catalog. The value is client-defined JSON,
        # opaque to the store (mirrors agentbus_consumer_cursors).
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipeline_cursors (
                scope TEXT NOT NULL,
                name TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope, name)
            );
            """
        )
        self._ensure_column(
            "agents", "attestation_key_ciphertext", "attestation_key_ciphertext TEXT"
        )
        self._ensure_column(
            "agents", "installed_packages", "installed_packages TEXT NOT NULL DEFAULT '{}'"
        )
        # schema_dispatch_hold: per-agent dispatch hold + zombie-detection counters.
        self._ensure_column(
            "agents", "dispatch_hold", "dispatch_hold INTEGER NOT NULL DEFAULT 0"
        )
        self._ensure_column(
            "agents", "dispatch_hold_reason", "dispatch_hold_reason TEXT"
        )
        self._ensure_column(
            "agents", "dispatch_hold_at", "dispatch_hold_at TEXT"
        )
        self._ensure_column(
            "agents",
            "consecutive_lease_expiries_no_telemetry",
            "consecutive_lease_expiries_no_telemetry INTEGER NOT NULL DEFAULT 0",
        )
        self._ensure_column(
            "agents", "last_control_stream_published_at", "last_control_stream_published_at TEXT"
        )
        self._ensure_column(
            "agents", "last_control_stream_consumed_at", "last_control_stream_consumed_at TEXT"
        )
        self._ensure_column("machines", "hardware", "hardware TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("tasks", "started_at", "started_at TEXT")
        self._ensure_column("tasks", "completed_at", "completed_at TEXT")
        self._ensure_column("tasks", "workflow_run_id", "workflow_run_id TEXT")
        self._ensure_column("tasks", "workflow_node_key", "workflow_node_key TEXT")
        # A break-glass authorization is deliberately outside task metadata:
        # ordinary task authors must never be able to request host execution by
        # writing a magic key into an otherwise untrusted task document.
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS task_break_glass_authorizations (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                execution_boundary TEXT NOT NULL,
                reason TEXT NOT NULL,
                authorized_by TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                claimed_at TEXT,
                lease_id TEXT REFERENCES leases(id) ON DELETE SET NULL,
                consumed_at TEXT,
                revoked_at TEXT,
                revoked_by TEXT,
                revoke_reason TEXT,
                CHECK(execution_boundary = 'host'),
                CHECK(status IN ('active', 'claimed', 'consumed', 'revoked', 'expired'))
            );
            CREATE INDEX IF NOT EXISTS idx_task_break_glass_task_status
                ON task_break_glass_authorizations (task_id, status, expires_at);
            CREATE INDEX IF NOT EXISTS idx_task_break_glass_agent_status
                ON task_break_glass_authorizations (agent_id, status, expires_at);
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_task_break_glass_active
                ON task_break_glass_authorizations (task_id)
                WHERE status = 'active';
            """
        )
        self._ensure_column(
            "workflow_runs", "next_action_at", "next_action_at TEXT"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_runs_next_action "
            "ON workflow_runs (state, next_action_at, id)"
        )
        # PR2c (spec §6.3, Option B): dispatcher (lease owner) may delegate
        # lifecycle authorship to the role agent spawned in the task Job.
        self._ensure_column("leases", "delegated_agent_id", "delegated_agent_id TEXT")
        # Lease expiry is a two-phase operation: the ACTIVE -> EXPIRED fence is
        # committed before failure classification and repair creation. Persist
        # a per-lease finalizer claim so concurrent sweepers cannot both run
        # those side effects; a stale claim is safely recoverable because the
        # repair identity and the final task CAS are idempotent.
        self._ensure_column(
            "leases", "expiry_finalizer_token", "expiry_finalizer_token TEXT"
        )
        self._ensure_column(
            "leases",
            "expiry_finalizer_claimed_at",
            "expiry_finalizer_claimed_at TEXT",
        )
        self._ensure_column(
            "leases", "expiry_finalized_at", "expiry_finalized_at TEXT"
        )
        self._ensure_column(
            "leases",
            "expiry_finalization_decision",
            "expiry_finalization_decision TEXT",
        )
        # Evidence artifact bytes may be externalized to the hub blob store
        # (mac.evidence_blobs); the row keeps digest + URI, content_base64 "".
        self._ensure_column(
            "evidence_artifacts", "content_uri", "content_uri TEXT NOT NULL DEFAULT ''"
        )
        # mac-src1: source release and desired-source tables may be absent on
        # databases created before this migration. CREATE TABLE IF NOT EXISTS is
        # safe on both fresh and pre-existing databases; the triggers/indexes
        # use IF NOT EXISTS too.  We run the same DDL blocks used in
        # _initialize so older DBs get the full schema on first open.
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_releases (
                id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                canonical_remote_url TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                canonical_ref TEXT NOT NULL,
                tree_digest TEXT NOT NULL,
                artifact_digest TEXT,
                image_digest TEXT,
                created_by TEXT NOT NULL,
                created_by_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                review_evidence_id TEXT REFERENCES evidence(id) ON DELETE SET NULL,
                publication_evidence_id TEXT REFERENCES evidence(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(repository_id, commit_sha),
                CHECK(length(commit_sha) = 40 AND commit_sha GLOB '[0-9a-f]*'),
                CHECK(canonical_ref NOT LIKE 'refs/heads/%')
            );
            CREATE INDEX IF NOT EXISTS idx_source_releases_repo_status
                ON source_releases (repository_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_source_releases_status_created
                ON source_releases (status, created_at);
            CREATE TRIGGER IF NOT EXISTS trg_source_releases_sha_immutable
            BEFORE UPDATE OF commit_sha ON source_releases
            FOR EACH ROW
            WHEN NEW.commit_sha != OLD.commit_sha
            BEGIN
                SELECT RAISE(ABORT, 'source_releases.commit_sha is immutable');
            END;

            CREATE TABLE IF NOT EXISTS fleet_desired_source_states (
                id TEXT PRIMARY KEY,
                fleet_id TEXT REFERENCES fleets(id) ON DELETE CASCADE,
                environment_id TEXT REFERENCES environments(id) ON DELETE CASCADE,
                generation INTEGER NOT NULL,
                release_id TEXT NOT NULL REFERENCES source_releases(id),
                rollout_policy TEXT NOT NULL DEFAULT 'immediate',
                actor TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                prior_generation INTEGER,
                paused INTEGER NOT NULL DEFAULT 0,
                request_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(generation >= 1),
                CHECK(fleet_id IS NOT NULL OR environment_id IS NOT NULL)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_desired_source_fleet
                ON fleet_desired_source_states (fleet_id) WHERE fleet_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_fleet_desired_source_env
                ON fleet_desired_source_states (environment_id) WHERE environment_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_fleet
                ON fleet_desired_source_states (fleet_id, generation);
            CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_env
                ON fleet_desired_source_states (environment_id, generation);
            CREATE TRIGGER IF NOT EXISTS trg_fleet_desired_source_gen_monotonic
            BEFORE UPDATE OF generation ON fleet_desired_source_states
            FOR EACH ROW
            WHEN NEW.generation <= OLD.generation
            BEGIN
                SELECT RAISE(ABORT, 'fleet_desired_source_states.generation must increase monotonically');
            END;

            CREATE TABLE IF NOT EXISTS fleet_desired_source_transitions (
                id TEXT PRIMARY KEY,
                desired_source_state_id TEXT NOT NULL
                    REFERENCES fleet_desired_source_states(id) ON DELETE CASCADE,
                from_generation INTEGER,
                to_generation INTEGER NOT NULL,
                release_id TEXT NOT NULL REFERENCES source_releases(id),
                rollout_policy TEXT NOT NULL DEFAULT 'immediate',
                actor TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                request_id TEXT,
                created_at TEXT NOT NULL,
                CHECK(to_generation >= 1)
            );
            CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_transitions_state
                ON fleet_desired_source_transitions (desired_source_state_id, to_generation);
            CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_transitions_release
                ON fleet_desired_source_transitions (release_id, created_at);

            CREATE TABLE IF NOT EXISTS fleet_desired_source_idempotency (
                id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                request_id TEXT NOT NULL,
                desired_source_state_id TEXT NOT NULL
                    REFERENCES fleet_desired_source_states(id) ON DELETE CASCADE,
                generation INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(scope_key, request_id)
            );
            CREATE INDEX IF NOT EXISTS idx_fleet_desired_source_idempotency_scope
                ON fleet_desired_source_idempotency (scope_key, request_id);

            CREATE TABLE IF NOT EXISTS evidence_reuse_records (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                source_evidence_id TEXT NOT NULL,
                remote_url TEXT,
                expected_head_sha TEXT,
                reused INTEGER NOT NULL,
                verification TEXT NOT NULL,
                problems TEXT NOT NULL,
                decided_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reused_by_agent_id TEXT NOT NULL DEFAULT '',
                reuse_context TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_reuse_records_task
                ON evidence_reuse_records (task_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_evidence_reuse_records_source
                ON evidence_reuse_records (source_evidence_id, created_at);

            CREATE TABLE IF NOT EXISTS source_convergence_nodes (
                id TEXT PRIMARY KEY,
                desired_source_state_id TEXT NOT NULL
                    REFERENCES fleet_desired_source_states(id) ON DELETE CASCADE,
                fleet_id TEXT NOT NULL REFERENCES fleets(id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                desired_generation INTEGER NOT NULL,
                release_id TEXT NOT NULL REFERENCES source_releases(id),
                desired_sha TEXT NOT NULL,
                actual_sha TEXT,
                action TEXT NOT NULL,
                plan_digest TEXT NOT NULL,
                phase TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                request_id TEXT,
                stream_id TEXT REFERENCES agentbus_streams(id) ON DELETE SET NULL,
                next_retry_at TEXT,
                blocker_code TEXT,
                blocker_detail TEXT,
                result TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(fleet_id, agent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_source_convergence_nodes_phase
                ON source_convergence_nodes (fleet_id, desired_generation, phase);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_source_convergence_nodes_request
                ON source_convergence_nodes (request_id) WHERE request_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS source_convergence_controller_leases (
                scope_key TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- Human principals registry: first-class assignable human identities
            -- (username / email / GitHub login) and explicit group membership.
            CREATE TABLE IF NOT EXISTS humans (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                email TEXT,
                github_login TEXT UNIQUE,
                display_name TEXT,
                groups TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_humans_username
                ON humans (username);
            CREATE INDEX IF NOT EXISTS idx_humans_github_login
                ON humans (github_login)
                WHERE github_login IS NOT NULL;

            CREATE TABLE IF NOT EXISTS human_groups (
                id TEXT PRIMARY KEY,
                human_id TEXT NOT NULL REFERENCES humans(id) ON DELETE CASCADE,
                group_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(human_id, group_name)
            );
            CREATE INDEX IF NOT EXISTS idx_human_groups_human
                ON human_groups (human_id);
            CREATE INDEX IF NOT EXISTS idx_human_groups_group
                ON human_groups (group_name);

            CREATE TABLE IF NOT EXISTS openclaw_conversation_executions (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                persona_instance_id TEXT NOT NULL,
                persona_id TEXT,
                agent_id TEXT,
                human_id TEXT NOT NULL,
                tenant_id TEXT,
                slack TEXT NOT NULL,
                repository TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                granted_capabilities TEXT NOT NULL DEFAULT '[]',
                task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                worktree TEXT,
                candidate_ref TEXT,
                candidate_sha TEXT,
                candidate_tree_digest TEXT,
                review_target_sha TEXT,
                gate_results TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_openclaw_conv_exec_persona
                ON openclaw_conversation_executions (persona_instance_id);
            CREATE INDEX IF NOT EXISTS idx_openclaw_conv_exec_task
                ON openclaw_conversation_executions (task_id);
            """
        )
        # Human assignees and identity stamp on tasks: human_assignees is a
        # JSON list of human ids/logins; created_by_human stamps the task's
        # creating human identity. Both are nullable so existing rows keep
        # their NULL values after the migration.
        self._ensure_column(
            "tasks", "human_assignees", "human_assignees TEXT"
        )
        self._ensure_column(
            "tasks", "created_by_human", "created_by_human TEXT"
        )
        self._ensure_column(
            "tasks", "idempotency_key", "idempotency_key TEXT"
        )
        self._ensure_column(
            "agent_crash_reports",
            "repair_attempt_count",
            "repair_attempt_count INTEGER NOT NULL DEFAULT 0",
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency_key"
            " ON tasks (idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        # Reuse provenance columns on evidence_reuse_records: added additively
        # so databases created before provenance recording keep their rows and
        # gain the new columns with empty/"{}" defaults.
        self._ensure_column(
            "evidence_reuse_records",
            "reused_by_agent_id",
            "reused_by_agent_id TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            "evidence_reuse_records",
            "reuse_context",
            "reuse_context TEXT NOT NULL DEFAULT ''",
        )
        self._ensure_column(
            "evidence_reuse_records",
            "metadata",
            "metadata TEXT NOT NULL DEFAULT '{}'",
        )
        self._ensure_column(
            "fleet_directive_activations",
            "deactivated_by",
            "deactivated_by TEXT",
        )
        self._ensure_column(
            "fleet_directive_activations",
            "deactivation_reason",
            "deactivation_reason TEXT",
        )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(%s)" % table)}
        if column not in columns:
            self._conn.execute("ALTER TABLE %s ADD COLUMN %s" % (table, definition))

    # ------------------------------------------------------------------
    # Human principals CRUD helpers
    # ------------------------------------------------------------------
    # These helpers mirror the style of the rest of SQLiteStore: callers
    # are responsible for JSON-serialising / deserialising list fields.
    # ``groups`` is stored as a JSON array text column.  The upsert also
    # reconciles the ``human_groups`` membership table so both the denorm
    # JSON column and the normalised table stay in sync.
    # ------------------------------------------------------------------

    def upsert_human(
        self,
        human_id: str,
        username: str,
        *,
        email: Optional[str] = None,
        github_login: Optional[str] = None,
        display_name: Optional[str] = None,
        groups: Optional[list] = None,
        created_at: str,
        updated_at: str,
    ) -> None:
        """Insert or replace a human row and reconcile group membership."""
        import json as _json

        groups_json = _json.dumps(sorted(set(groups or [])))
        self.execute(
            """
            INSERT INTO humans (id, username, email, github_login, display_name,
                                groups, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username     = excluded.username,
                email        = excluded.email,
                github_login = excluded.github_login,
                display_name = excluded.display_name,
                groups       = excluded.groups,
                updated_at   = excluded.updated_at
            """,
            (
                human_id,
                username,
                email,
                github_login,
                display_name,
                groups_json,
                created_at,
                updated_at,
            ),
        )
        # Reconcile human_groups: remove rows no longer in the groups list,
        # then insert any new ones (idempotent via INSERT OR IGNORE).
        current_groups = sorted(set(groups or []))
        self.execute(
            "DELETE FROM human_groups WHERE human_id = ?", (human_id,)
        )
        for group_name in current_groups:
            self.execute(
                """
                INSERT OR IGNORE INTO human_groups (id, human_id, group_name, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "hg_%s_%s" % (human_id, group_name),
                    human_id,
                    group_name,
                    created_at,
                ),
            )

    def get_human(self, human_id: str) -> Optional[Any]:
        """Return the human row for ``human_id``, or None if not found."""
        return self.query_one(
            "SELECT * FROM humans WHERE id = ?", (human_id,)
        )

    def get_human_by_username(self, username: str) -> Optional[Any]:
        """Return the human row for ``username``, or None if not found."""
        return self.query_one(
            "SELECT * FROM humans WHERE username = ?", (username,)
        )

    def list_humans(self, *, group: Optional[str] = None) -> list:
        """Return all humans, optionally filtered by group membership."""
        if group is not None:
            return self.query_all(
                """
                SELECT h.* FROM humans h
                INNER JOIN human_groups hg ON hg.human_id = h.id
                WHERE hg.group_name = ?
                ORDER BY h.username
                """,
                (group,),
            )
        return self.query_all("SELECT * FROM humans ORDER BY username")

    def delete_human(self, human_id: str) -> bool:
        """Delete a human by id; returns True if a row was deleted."""
        cursor = self.execute(
            "DELETE FROM humans WHERE id = ?", (human_id,)
        )
        return cursor.rowcount > 0
