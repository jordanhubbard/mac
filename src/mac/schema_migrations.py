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
FORMER_STARTUP_ENSURE_COLUMNS = frozenset(
    {
        ("agents", "installed_packages"),
        ("agents", "attestation_key_prev_ciphertext"),
        ("agents", "attestation_key_history_ciphertext"),
        ("fleet_release_epoch_agents", "prior_report_executor_projection_sha256"),
        ("fleet_release_epochs", "abort_disposition"),
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
        ("fleet_release_admission_episodes", "project"),
        ("fleet_release_admission_episodes", "barrier_resource_digest"),
        ("fleet_release_admission_episodes", "owner_kind"),
        ("fleet_release_admission_episodes", "owner_id"),
        ("fleet_release_admission_episodes", "waiter_kind"),
        ("fleet_release_admission_episodes", "waiter_id"),
        ("fleet_release_admission_episodes", "waiting_publishers"),
        ("fleet_release_admission_episodes", "waiting_epoch_openers"),
        ("fleet_release_admission_episodes", "queue_depth"),
        ("fleet_release_admission_episodes", "wait_started_at"),
        ("fleet_release_admission_episodes", "wait_ended_at"),
        ("fleet_release_admission_episodes", "wait_seconds"),
        ("fleet_release_admission_episodes", "outcome"),
        ("fleet_release_admission_episodes", "metadata"),
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
        """
        SELECT to_regclass(current_schema() || '.dream_runs') IS NOT NULL
           AND to_regclass(current_schema() || '.dream_candidate_entries') IS NOT NULL
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


def _expected_inventory(sql: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    tables = {table: _column_names(body) for table, body in _table_bodies(sql).items()}
    for match in re.finditer(
        r"ALTER TABLE\s+(\w+)\s+ADD COLUMN IF NOT EXISTS\s+(\w+)", sql, re.IGNORECASE
    ):
        if match.group(1) in tables:
            tables[match.group(1)].add(match.group(2))
    objects = {
        "indexes": set(re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS\s+(\w+)", sql)),
        "triggers": set(re.findall(r"CREATE TRIGGER\s+(\w+)", sql)),
        "views": set(re.findall(r"CREATE OR REPLACE VIEW\s+(\w+)", sql)),
        "functions": set(
            re.findall(r"CREATE OR REPLACE FUNCTION\s+(\w+)\s*\(", sql, re.IGNORECASE)
        ),
    }
    return tables, objects


def _later_migration_tables(migrations: Sequence[Migration]) -> set[str]:
    """Tables an unversioned install may already have from legacy lazy DDL."""

    tables: set[str] = set()
    for migration in migrations[1:]:
        tables.update(_table_bodies(migration.sql))
    return tables


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
                    }
                proof = _prove_baseline(
                    cur,
                    migrations[0].sql,
                    allowed_extra_tables=_later_migration_tables(migrations),
                )
                return {
                    "status": "pending",
                    "database_state": "existing-unversioned",
                    "current_version": None,
                    "pending": [migration.migration_id for migration in migrations],
                    "requires_backup": True,
                    "requires_existing_baseline_authority": True,
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

                if not presence:
                    user_relations = relations_before - AUTHORITY_TABLES
                    if user_relations:
                        if not authorize_existing_baseline:
                            raise StoreError(
                                "existing unversioned PostgreSQL schema requires explicit "
                                "--authorize-existing-baseline after backup and quiesce"
                            )
                        baseline_proof = _prove_baseline(
                            cur,
                            migrations[0].sql,
                            allowed_extra_tables=_later_migration_tables(migrations),
                        )
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
            )
        print(__import__("json").dumps(result, sort_keys=True))
    except StoreError as exc:
        parser.exit(1, "mac-schema-migrate: %s\n" % exc)
    finally:
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
