"""Integrity-verified, resumable SQLite to PostgreSQL authority migration.

The migrator is intentionally an offline control-plane operation. It takes an
exclusive SQLite transaction after checkpointing WAL, requires an empty
PostgreSQL target (unless resuming its own receipt), copies one table per
transaction, and verifies deterministic full-row digests plus foreign keys.
It never logs or persists the PostgreSQL DSN.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence


REPORT_SCHEMA = "mac.sqlite_postgres_migration.v1"
EMPTY_LEGACY_SOURCE_TABLES = frozenset({"task_lifecycle_outbox"})


class SQLitePostgresMigrationError(RuntimeError):
    """The authority migration cannot be completed or proved correct."""


@dataclass(frozen=True)
class TablePlan:
    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    destination_types: Mapping[str, str]


def _quote_sqlite(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _timestamp_bytes(value: Any) -> bytes:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return str(value).encode("utf-8")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").encode("ascii")


def _canonical_value(value: Any, destination_type: str) -> bytes:
    if value is None:
        return b"n"
    type_name = str(destination_type or "").lower()
    if type_name == "boolean":
        return b"i1" if bool(value) else b"i0"
    if type_name in {"json", "jsonb"}:
        decoded = value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                decoded = value
        return b"j" + _json_bytes(decoded)
    if "timestamp" in type_name:
        return b"t" + _timestamp_bytes(value)
    if isinstance(value, bool):
        return b"i1" if value else b"i0"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii")
    if isinstance(value, float):
        if math.isnan(value):
            return b"fnan"
        if math.isinf(value):
            return b"f+inf" if value > 0 else b"f-inf"
        return b"f" + value.hex().encode("ascii")
    if isinstance(value, Decimal):
        return b"d" + format(value, "f").encode("ascii")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return b"b" + bytes(value)
    if isinstance(value, (dict, list)):
        return b"j" + _json_bytes(value)
    return b"s" + str(value).encode("utf-8")


def _update_row_digest(
    digest: "hashlib._Hash",
    row: Sequence[Any],
    column_types: Sequence[str],
) -> None:
    digest.update(len(row).to_bytes(4, "big"))
    for value, type_name in zip(row, column_types):
        encoded = _canonical_value(value, type_name)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)


def _source_tables(conn: sqlite3.Connection) -> List[str]:
    return [
        str(row[0])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]


def _migration_source_tables(conn: sqlite3.Connection) -> tuple[List[str], List[str]]:
    """Select current authority tables while proving named legacy tables empty."""
    migrated: List[str] = []
    skipped: List[str] = []
    for table in _source_tables(conn):
        if table not in EMPTY_LEGACY_SOURCE_TABLES:
            migrated.append(table)
            continue
        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM %s" % _quote_sqlite(table)
            ).fetchone()[0]
        )
        if count:
            raise SQLitePostgresMigrationError(
                "legacy SQLite-only table %s contains %d row(s); "
                "refusing to omit data" % (table, count)
            )
        skipped.append(table)
    return migrated, skipped


def _source_columns(
    conn: sqlite3.Connection, table: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rows = list(conn.execute("PRAGMA table_info(%s)" % _quote_sqlite(table)))
    columns = tuple(str(row[1]) for row in rows)
    primary_key = tuple(
        str(row[1])
        for row in sorted(
            (row for row in rows if int(row[5] or 0) > 0),
            key=lambda row: int(row[5]),
        )
    )
    return columns, primary_key


def _source_dependencies(conn: sqlite3.Connection, tables: Iterable[str]) -> Dict[str, set[str]]:
    names = set(tables)
    return {
        table: {
            str(row[2])
            for row in conn.execute("PRAGMA foreign_key_list(%s)" % _quote_sqlite(table))
            if str(row[2]) in names and str(row[2]) != table
        }
        for table in names
    }


def _topological_tables(conn: sqlite3.Connection, tables: Iterable[str]) -> List[str]:
    remaining = set(tables)
    dependencies = _source_dependencies(conn, remaining)
    ordered: List[str] = []
    while remaining:
        ready = sorted(table for table in remaining if not (dependencies[table] & remaining))
        if not ready:
            raise SQLitePostgresMigrationError(
                "SQLite foreign-key graph contains a cycle: %s" % ", ".join(sorted(remaining))
            )
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def _atomic_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _target_fingerprint(dsn: str) -> str:
    try:
        from psycopg.conninfo import conninfo_to_dict

        values = conninfo_to_dict(dsn)
    except Exception:
        values = {"dsn_scheme": str(dsn).split(":", 1)[0]}
    values.pop("password", None)
    return hashlib.sha256(_json_bytes(values)).hexdigest()


def _destination_columns(conn: Any, table: str) -> Dict[str, str]:
    rows = conn.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def _row_query(table: str, columns: Sequence[str], order: Sequence[str]) -> str:
    projection = ", ".join(_quote_sqlite(column) for column in columns)
    sql = "SELECT %s FROM %s" % (projection, _quote_sqlite(table))
    ordering = tuple(order) or tuple(columns)
    if ordering:
        sql += " ORDER BY " + ", ".join(_quote_sqlite(column) for column in ordering)
    return sql


def _source_table_digest(
    conn: sqlite3.Connection,
    plan: TablePlan,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    types = [plan.destination_types[column] for column in plan.columns]
    cursor = conn.execute(_row_query(plan.name, plan.columns, plan.primary_key))
    while True:
        rows = cursor.fetchmany(4096)
        if not rows:
            break
        for row in rows:
            _update_row_digest(digest, row, types)
            count += 1
    return count, digest.hexdigest()


def _destination_table_digest(conn: Any, plan: TablePlan) -> tuple[int, str]:
    from psycopg import sql

    ordering = plan.primary_key or plan.columns
    query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        sql.SQL(", ").join(sql.Identifier(column) for column in plan.columns),
        sql.Identifier(plan.name),
        sql.SQL(", ").join(sql.Identifier(column) for column in ordering),
    )
    digest = hashlib.sha256()
    count = 0
    types = [plan.destination_types[column] for column in plan.columns]
    # Server-side cursors keep multi-million-row telemetry tables bounded in
    # memory. They require an explicit transaction even on our autocommit
    # migration connection.
    with conn.transaction():
        with conn.cursor(name="mac_migration_digest_%s" % plan.name) as cursor:
            cursor.itersize = 4096
            cursor.execute(query)
            for row in cursor:
                _update_row_digest(digest, row, types)
                count += 1
    return count, digest.hexdigest()


def _adapt_copy_row(row: Sequence[Any], plan: TablePlan) -> tuple[Any, ...]:
    adapted: List[Any] = []
    for value, column in zip(row, plan.columns):
        type_name = plan.destination_types[column].lower()
        if value is not None and type_name == "boolean":
            adapted.append(bool(value))
        else:
            adapted.append(value)
    return tuple(adapted)


def _copy_table(
    source: sqlite3.Connection,
    target: Any,
    plan: TablePlan,
    *,
    batch_size: int,
) -> tuple[int, str]:
    from psycopg import sql

    digest = hashlib.sha256()
    count = 0
    types = [plan.destination_types[column] for column in plan.columns]
    select_cursor = source.execute(_row_query(plan.name, plan.columns, plan.primary_key))
    copy_sql = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(plan.name),
        sql.SQL(", ").join(sql.Identifier(column) for column in plan.columns),
    )
    with target.cursor().copy(copy_sql) as copy:
        while True:
            rows = select_cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                _update_row_digest(digest, row, types)
                copy.write_row(_adapt_copy_row(row, plan))
                count += 1
    return count, digest.hexdigest()


def _source_foreign_keys(conn: sqlite3.Connection, tables: Iterable[str]) -> list:
    result = []
    for table in tables:
        grouped: Dict[int, list] = {}
        for row in conn.execute("PRAGMA foreign_key_list(%s)" % _quote_sqlite(table)):
            grouped.setdefault(int(row[0]), []).append(row)
        for fk_id, rows in sorted(grouped.items()):
            ordered = sorted(rows, key=lambda row: int(row[1]))
            parent = str(ordered[0][2])
            child_columns = [str(row[3]) for row in ordered]
            parent_columns = [str(row[4] or "") for row in ordered]
            if any(not column for column in parent_columns):
                _, parent_pk = _source_columns(conn, parent)
                parent_columns = list(parent_pk)
            result.append(
                {
                    "table": table,
                    "id": fk_id,
                    "parent": parent,
                    "child_columns": child_columns,
                    "parent_columns": parent_columns,
                }
            )
    return result


def _verify_destination_foreign_keys(conn: Any, foreign_keys: Iterable[Mapping[str, Any]]) -> int:
    from psycopg import sql

    checked = 0
    for relation in foreign_keys:
        child_columns = list(relation["child_columns"])
        parent_columns = list(relation["parent_columns"])
        if len(child_columns) != len(parent_columns) or not child_columns:
            raise SQLitePostgresMigrationError(
                "cannot verify foreign key %s/%s" % (relation["table"], relation["id"])
            )
        nonnull = sql.SQL(" AND ").join(
            sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(column)) for column in child_columns
        )
        equality = sql.SQL(" AND ").join(
            sql.SQL("p.{} = c.{}").format(
                sql.Identifier(parent),
                sql.Identifier(child),
            )
            for child, parent in zip(child_columns, parent_columns)
        )
        query = sql.SQL(
            "SELECT COUNT(*) FROM {} c WHERE {} AND NOT EXISTS (SELECT 1 FROM {} p WHERE {})"
        ).format(
            sql.Identifier(str(relation["table"])),
            nonnull,
            sql.Identifier(str(relation["parent"])),
            equality,
        )
        orphan_count = int(conn.execute(query).fetchone()[0])
        if orphan_count:
            raise SQLitePostgresMigrationError(
                "PostgreSQL foreign key %s/%s has %d orphan row(s)"
                % (relation["table"], relation["id"], orphan_count)
            )
        checked += 1
    return checked


def _reset_sequences(conn: Any, plans: Iterable[TablePlan]) -> int:
    from psycopg import sql

    reset = 0
    for plan in plans:
        for column in plan.columns:
            sequence_row = conn.execute(
                "SELECT pg_get_serial_sequence(%s, %s)",
                (plan.name, column),
            ).fetchone()
            sequence = sequence_row[0] if sequence_row else None
            if not sequence:
                continue
            maximum = conn.execute(
                sql.SQL("SELECT MAX({}) FROM {}").format(
                    sql.Identifier(column),
                    sql.Identifier(plan.name),
                )
            ).fetchone()[0]
            conn.execute(
                "SELECT setval(%s, %s, %s)",
                (sequence, int(maximum or 1), maximum is not None),
            )
            reset += 1
    return reset


def _load_report(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SQLitePostgresMigrationError(
            "cannot read migration receipt %s: %s" % (path, exc)
        ) from exc
    if not isinstance(value, dict) or value.get("schema") != REPORT_SCHEMA:
        raise SQLitePostgresMigrationError("migration receipt schema is invalid")
    return value


def migrate_sqlite_to_postgres(
    sqlite_path: str | Path,
    postgres_dsn: str,
    *,
    report_path: str | Path,
    batch_size: int = 10_000,
    resume: bool = False,
    restart: bool = False,
    verify_only: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Copy and prove a complete SQLite authority in PostgreSQL.

    The caller must stop all hub writer processes before invoking this
    function. The exclusive SQLite transaction protects the source snapshot
    during the copy, but it cannot prevent a stopped service from being
    restarted with the old SQLite authority after this function returns.
    """
    source_path = Path(sqlite_path).expanduser().resolve()
    receipt_path = Path(report_path).expanduser().resolve()
    if not source_path.is_file():
        raise SQLitePostgresMigrationError("SQLite authority does not exist: %s" % source_path)
    if not str(postgres_dsn or "").startswith(("postgres://", "postgresql://")):
        raise SQLitePostgresMigrationError("PostgreSQL DSN must use postgres:// or postgresql://")
    if sum(bool(value) for value in (resume, restart, verify_only)) > 1:
        raise SQLitePostgresMigrationError(
            "--resume, --restart, and --verify-only are mutually exclusive"
        )
    batch_size = min(max(1, int(batch_size)), 100_000)
    announce = progress or (lambda _message: None)

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - package dependency guard
        raise SQLitePostgresMigrationError("psycopg is required for PostgreSQL migration") from exc

    # Apply the exact application schema only for a fresh/restarted target.
    # Re-running data-bearing schema backfills while resuming a partially
    # copied authority could synthesize rows from the already-copied prefix.
    if not (resume or verify_only):
        from mac.store_postgres import PostgresStore

        initialized = PostgresStore(postgres_dsn, pool_size=1)
        try:
            initialized.initialize()
        finally:
            initialized.close()

    source = sqlite3.connect(
        str(source_path),
        timeout=30.0,
        isolation_level=None,
    )
    source.row_factory = sqlite3.Row
    target = psycopg.connect(postgres_dsn, autocommit=True)
    try:
        checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and int(checkpoint[0] or 0) != 0:
            raise SQLitePostgresMigrationError(
                "SQLite WAL checkpoint is busy; stop every hub writer and retry"
            )
        source.execute("BEGIN EXCLUSIVE")
        integrity = str(source.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise SQLitePostgresMigrationError("SQLite integrity_check failed: %s" % integrity)
        foreign_key_rows = list(source.execute("PRAGMA foreign_key_check"))
        if foreign_key_rows:
            raise SQLitePostgresMigrationError(
                "SQLite foreign_key_check found %d violation(s)" % len(foreign_key_rows)
            )
        source_sha256 = _sha256_file(source_path)
        target_fingerprint = _target_fingerprint(postgres_dsn)
        source_tables, skipped_empty_legacy_tables = _migration_source_tables(source)
        tables = _topological_tables(source, source_tables)
        source_telemetry_versions = {
            str(row[0]) for row in source.execute("SELECT version FROM telemetry_data_migrations")
        }
        target_bootstrap_versions = {
            str(row[0]) for row in target.execute("SELECT version FROM telemetry_data_migrations")
        }
        missing_bootstrap_versions = target_bootstrap_versions - source_telemetry_versions
        if missing_bootstrap_versions:
            raise SQLitePostgresMigrationError(
                "SQLite authority predates PostgreSQL data migrations: %s"
                % ", ".join(sorted(missing_bootstrap_versions))
            )
        plans: List[TablePlan] = []
        for table in tables:
            columns, primary_key = _source_columns(source, table)
            destination = _destination_columns(target, table)
            if not destination:
                raise SQLitePostgresMigrationError(
                    "PostgreSQL schema is missing source table %s" % table
                )
            if set(columns) != set(destination):
                raise SQLitePostgresMigrationError(
                    "column mismatch for %s: SQLite=%s PostgreSQL=%s"
                    % (table, sorted(columns), sorted(destination))
                )
            plans.append(TablePlan(table, columns, primary_key, destination))

        if restart:
            announce("Clearing PostgreSQL target for a fresh migration")
            from psycopg import sql

            with target.transaction():
                target.execute(
                    sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                        sql.SQL(", ").join(sql.Identifier(plan.name) for plan in plans)
                    )
                )

        report: Dict[str, Any]
        if resume or verify_only:
            report = _load_report(receipt_path)
            if report.get("source_sha256") != source_sha256:
                raise SQLitePostgresMigrationError(
                    "SQLite authority changed since the migration receipt"
                )
            if report.get("target_fingerprint") != target_fingerprint:
                raise SQLitePostgresMigrationError(
                    "PostgreSQL target differs from the migration receipt"
                )
            if report.get("skipped_empty_legacy_tables") != skipped_empty_legacy_tables:
                raise SQLitePostgresMigrationError(
                    "legacy SQLite table disposition differs from the migration receipt"
                )
        else:
            nonempty = []
            for plan in plans:
                count = int(
                    target.execute(
                        'SELECT COUNT(*) FROM "%s"' % plan.name.replace('"', '""')
                    ).fetchone()[0]
                )
                if count:
                    nonempty.append("%s=%d" % (plan.name, count))
            unsupported_nonempty = [
                item for item in nonempty if not item.startswith("telemetry_data_migrations=")
            ]
            if unsupported_nonempty:
                raise SQLitePostgresMigrationError(
                    "PostgreSQL target is not empty; use --resume with its receipt "
                    "or explicitly --restart: %s" % ", ".join(unsupported_nonempty[:12])
                )
            report = {
                "schema": REPORT_SCHEMA,
                "status": "copying",
                "source_path": str(source_path),
                "source_bytes": source_path.stat().st_size,
                "source_sha256": source_sha256,
                "source_integrity_check": integrity,
                "source_foreign_key_violations": 0,
                "skipped_empty_legacy_tables": skipped_empty_legacy_tables,
                "target_fingerprint": target_fingerprint,
                "target_bootstrap_telemetry_versions": sorted(target_bootstrap_versions),
                "tables": {},
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_report(receipt_path, report)

        table_reports = report.setdefault("tables", {})
        if not isinstance(table_reports, dict):
            raise SQLitePostgresMigrationError("migration receipt tables field is invalid")
        total_rows = 0
        for index, plan in enumerate(plans, start=1):
            prior = table_reports.get(plan.name)
            if isinstance(prior, dict) and prior.get("status") == "verified":
                announce("[%d/%d] verifying resumed table %s" % (index, len(plans), plan.name))
                destination_count, destination_digest = _destination_table_digest(target, plan)
                if destination_count != int(
                    prior.get("rows") or 0
                ) or destination_digest != prior.get("sha256"):
                    raise SQLitePostgresMigrationError(
                        "resumed PostgreSQL table %s no longer matches its receipt" % plan.name
                    )
                total_rows += destination_count
                continue
            if verify_only:
                raise SQLitePostgresMigrationError(
                    "migration receipt has no verified entry for %s" % plan.name
                )
            existing = int(
                target.execute(
                    'SELECT COUNT(*) FROM "%s"' % plan.name.replace('"', '""')
                ).fetchone()[0]
            )
            if plan.name == "telemetry_data_migrations" and existing:
                # PostgreSQL schema initialization records the same one-time
                # backfills before user data is copied. Keep those markers in
                # place during the copy (so a crash cannot retrigger a
                # backfill), then atomically replace them with the SQLite
                # authority's exact receipted rows.
                with target.transaction():
                    target.execute("TRUNCATE TABLE telemetry_data_migrations")
                existing = 0
            if existing:
                raise SQLitePostgresMigrationError(
                    "unreceipted PostgreSQL table %s contains %d row(s); "
                    "restart the migration rather than merging authorities" % (plan.name, existing)
                )
            announce("[%d/%d] copying %s" % (index, len(plans), plan.name))
            try:
                with target.transaction():
                    source_count, source_digest = _copy_table(
                        source,
                        target,
                        plan,
                        batch_size=batch_size,
                    )
                destination_count, destination_digest = _destination_table_digest(target, plan)
                if source_count != destination_count or source_digest != destination_digest:
                    with target.transaction():
                        target.execute('TRUNCATE TABLE "%s"' % plan.name.replace('"', '""'))
                    raise SQLitePostgresMigrationError(
                        "full-row verification failed for %s "
                        "(SQLite %d/%s, PostgreSQL %d/%s)"
                        % (
                            plan.name,
                            source_count,
                            source_digest,
                            destination_count,
                            destination_digest,
                        )
                    )
            except Exception:
                report["status"] = "failed"
                report["failed_table"] = plan.name
                report["failed_at"] = datetime.now(timezone.utc).isoformat()
                _atomic_report(receipt_path, report)
                raise
            table_reports[plan.name] = {
                "status": "verified",
                "rows": source_count,
                "sha256": source_digest,
            }
            total_rows += source_count
            report["completed_table_count"] = len(table_reports)
            report["copied_rows"] = total_rows
            report.pop("failed_table", None)
            report.pop("failed_at", None)
            _atomic_report(receipt_path, report)

        foreign_keys = _source_foreign_keys(source, tables)
        checked_foreign_keys = _verify_destination_foreign_keys(target, foreign_keys)
        with target.transaction():
            sequences = _reset_sequences(target, plans)
        report.update(
            {
                "status": "verified",
                "completed_table_count": len(plans),
                "copied_rows": total_rows,
                "verified_foreign_key_count": checked_foreign_keys,
                "reset_sequence_count": sequences,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        _atomic_report(receipt_path, report)
        source.execute("COMMIT")
        return report
    except Exception:
        try:
            source.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        target.close()
        source.close()


__all__ = [
    "REPORT_SCHEMA",
    "SQLitePostgresMigrationError",
    "TablePlan",
    "migrate_sqlite_to_postgres",
]
