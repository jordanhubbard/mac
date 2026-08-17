"""PostgreSQL backend for the Store protocol.

`PostgresStore` accepts SQL written in SQLite dialect — the same SQL strings
the rest of the codebase already uses — and translates `?` placeholders to
psycopg's `%s` format on the way to the driver. SQLite-style JSON helpers
(`json_extract`, `json_set`, etc.) are not handled here; they are provided
as SQL functions inside the Postgres schema (Phase 3.3) so call sites stay
unchanged.

Connections are borrowed from a `psycopg_pool.ConnectionPool` per call and
returned immediately. `execute()` buffers result rows eagerly so the
connection is safe to release before the caller fetches them. Inside a
`transaction()` block the same connection is held for the duration of the
``with`` and the wrapped `execute()` re-uses it.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import re
from typing import Any, Iterable, Iterator, List, Optional, Sequence, Tuple

from mac.store import StoreConnection, StoreError

SCHEMA_PATH = (
    Path(__file__).resolve().parent / "data" / "postgres" / "schema.sql"
)

try:
    import psycopg
    from psycopg_pool import ConnectionPool
except ImportError as exc:  # pragma: no cover - exercised by env check
    raise ImportError(
        "PostgresStore requires psycopg. Install via 'pip install \"mac[postgres]\"'."
    ) from exc


def _redact_dsn(dsn: str) -> str:
    """Redact any password from a Postgres DSN for safe reporting.

    Handles the two common shapes: a ``postgres://user:pass@host/db`` URL and a
    keyword ``password=...`` DSN. Anything else is returned unchanged. Only the
    password is removed; host, port, and database stay so operators can still
    identify the target cluster in a ``mac admin diagnostics`` report.
    """
    if not dsn:
        return dsn
    if "://" in dsn:
        try:
            from urllib.parse import urlsplit, urlunsplit

            parts = urlsplit(dsn)
            if parts.password is not None:
                userinfo = parts.username or ""
                host = parts.hostname or ""
                if parts.port is not None:
                    host = "%s:%d" % (host, parts.port)
                netloc = ("%s:***@%s" % (userinfo, host)) if userinfo else ("***@%s" % host)
                return urlunsplit(
                    (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
                )
            return dsn
        except Exception:
            return "postgres://<redacted>"
    return re.sub(r"(password=)([^\s]+)", r"\1***", dsn)


def _translate_placeholders(sql: str) -> str:
    """Translate SQLite-dialect SQL to psycopg `format` paramstyle.

    Inside SQL string literals (delimited by single quotes, with ``''`` as
    the escape) every character is preserved verbatim except that ``%`` is
    doubled to ``%%`` so psycopg does not treat it as a format-spec.

    Outside string literals:
      * ``?`` becomes ``%s`` (placeholder)
      * ``%`` becomes ``%%`` (literal — e.g. LIKE wildcards)

    Other quoting contexts (double-quoted identifiers, dollar-quoted bodies)
    are not entered by current service-layer SQL; if a future caller needs
    them, extend this function.
    """
    # SQLite's INDEXED BY is a query-local planner hint. PostgreSQL has no
    # equivalent syntax and its partial-index planner can select the same index
    # from the predicate, so remove the hint before placeholder translation.
    sql = re.sub(r"\s+INDEXED\s+BY\s+[A-Za-z_][A-Za-z0-9_]*", "", sql, flags=re.I)
    out: List[str] = []
    i = 0
    n = len(sql)
    in_string = False
    while i < n:
        ch = sql[i]
        if in_string:
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("''")
                    i += 2
                    continue
                in_string = False
                out.append("'")
                i += 1
                continue
            if ch == "%":
                out.append("%%")
            else:
                out.append(ch)
            i += 1
        else:
            if ch == "'":
                in_string = True
                out.append("'")
                i += 1
                continue
            if ch == "?":
                out.append("%s")
                i += 1
                continue
            if ch == "%":
                out.append("%%")
                i += 1
                continue
            out.append(ch)
            i += 1
    return "".join(out)


class _Row:
    """Result row supporting both ``row['col']`` and ``row[i]`` indexing.

    Mirrors `sqlite3.Row` so service-layer call sites that mix dict-style
    and positional access against the current SQLite backend keep working
    unchanged against Postgres.
    """

    __slots__ = ("_cols", "_values", "_lookup")

    def __init__(
        self, cols: Tuple[str, ...], values: Tuple[Any, ...]
    ) -> None:
        self._cols = cols
        self._values = values
        self._lookup = {c: i for i, c in enumerate(cols)}

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            return self._values[self._lookup[key]]
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._cols)

    def __len__(self) -> int:
        return len(self._cols)

    def keys(self) -> List[str]:
        return list(self._cols)

    def get(self, key: str, default: Any = None) -> Any:
        idx = self._lookup.get(key)
        if idx is None:
            return default
        return self._values[idx]

    def __contains__(self, key: object) -> bool:
        return key in self._lookup

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        body = ", ".join(
            "%s=%r" % (c, self._values[i]) for i, c in enumerate(self._cols)
        )
        return "Row(%s)" % body


class _Result:
    """Buffered query result; matches the slice of cursor API callers use.

    `lastrowid` is present but always None — Postgres has no implicit
    last-row-id concept. Code that needs an autogenerated id must use
    ``INSERT ... RETURNING <col>`` and read it from ``fetchone()``.
    The attribute exists so a caller accidentally reading it surfaces
    a clean TypeError (``int(None)``) instead of a confusing
    AttributeError; it is NEVER populated.
    """

    __slots__ = ("rowcount", "lastrowid", "_rows")

    def __init__(self, rowcount: int, rows: List[_Row]) -> None:
        self.rowcount = rowcount
        self.lastrowid = None
        self._rows = rows

    def fetchone(self) -> Optional[_Row]:
        if not self._rows:
            return None
        return self._rows.pop(0)

    def fetchall(self) -> List[_Row]:
        rows = self._rows
        self._rows = []
        return rows

    def __iter__(self) -> Iterator[_Row]:
        rows = self._rows
        self._rows = []
        return iter(rows)


def _materialize(cur: Any) -> Tuple[int, List[_Row]]:
    description = cur.description
    if not description:
        return cur.rowcount or 0, []
    cols = tuple(c.name for c in description)
    rows = [_Row(cols, tuple(raw)) for raw in cur.fetchall()]
    return cur.rowcount or len(rows), rows


class _Transaction:
    """``StoreConnection`` adapter used inside a ``transaction()`` block."""

    __slots__ = ("_conn",)

    def __init__(self, conn: "psycopg.Connection") -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Result:
        translated = _translate_placeholders(sql)
        try:
            with self._conn.cursor() as cur:
                cur.execute(translated, tuple(params) if params else None)
                rowcount, rows = _materialize(cur)
                return _Result(rowcount, rows)
        except psycopg.Error as exc:
            raise StoreError(str(exc)) from exc


from mac.store_helpers import StoreHelpersMixin


def _load_packaged_schema() -> str:
    """Read the bundled Postgres schema DDL from `src/mac/data/postgres/`.

    Matches the path-based loader pattern used by `roles_service.SEED_CATALOG`.
    Apply it once per fresh database via `PostgresStore.initialize()`.
    """
    if not SCHEMA_PATH.exists():  # pragma: no cover - packaging guard
        raise StoreError(
            "Postgres schema.sql is missing at %s; the wheel's "
            "force-include for src/mac/data may be misconfigured."
            % SCHEMA_PATH
        )
    return SCHEMA_PATH.read_text()


class PostgresStore(StoreHelpersMixin):
    """Postgres backend implementing the `Store` protocol.

    Construction opens the connection pool eagerly. The pool size is
    capped by `pool_size`; a single long-lived pool is shared across
    threads.
    """

    def __init__(
        self,
        dsn: str,
        *,
        pool_size: int = 10,
        min_size: int = 1,
    ) -> None:
        self.path = dsn
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=pool_size,
            open=True,
            kwargs={"client_encoding": "UTF8"},
        )

    #: Why the encoding is stated rather than inherited:
    #:
    #: libpq derives client_encoding from the process locale when the
    #: connection does not say. A LaunchDaemon has no LANG, so the hub
    #: negotiated SQL_ASCII and psycopg then encoded every statement as ASCII:
    #:
    #:     UnicodeEncodeError: 'ascii' codec can't encode character '\xa7'
    #:         in position 17789: ordinal not in range(128)
    #:
    #: Observed live on the hub, failing a review whose evidence contained a
    #: section sign. Agent output routinely carries non-ASCII -- box drawing,
    #: check marks, em dashes, any prose -- so this rejects real work at
    #: random, and only where the server happens to run without a locale. Every
    #: database in that cluster is UTF8; nothing was wrong with the data.

    def initialize(self) -> None:
        """Apply the bundled Postgres schema (idempotent).

        Equivalent to `SQLiteStore._initialize()` for the Postgres backend:
        creates all tables, indexes, triggers, the `events` view, and the
        `json_extract` SQL-function dialect shim. Safe to call on an
        already-initialised database — every statement uses
        `IF NOT EXISTS` or `OR REPLACE`.
        """
        schema = _load_packaged_schema()
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(schema)
        except psycopg.Error as exc:
            raise StoreError(str(exc)) from exc
        # Additive migrations for existing databases (idempotent via IF NOT EXISTS).
        self.ensure_column(
            "agents", "installed_packages", "installed_packages TEXT NOT NULL DEFAULT '{}'"
        )
        self.ensure_column(
            "agents",
            "attestation_key_prev_ciphertext",
            "attestation_key_prev_ciphertext TEXT",
        )
        self.ensure_column(
            "agents",
            "attestation_key_history_ciphertext",
            "attestation_key_history_ciphertext TEXT",
        )
        self.ensure_column(
            "fleet_release_epoch_agents",
            "prior_report_executor_projection_sha256",
            "prior_report_executor_projection_sha256 TEXT",
        )
        self.ensure_column(
            "fleet_release_epochs",
            "abort_disposition",
            "abort_disposition TEXT",
        )
        self.ensure_column("tasks", "human_assignees", "human_assignees TEXT")
        self.ensure_column("tasks", "created_by_human", "created_by_human TEXT")
        self.ensure_column("tasks", "idempotency_key", "idempotency_key TEXT")
        # schema_dispatch_hold: per-agent dispatch hold + zombie-detection counters.
        self.ensure_column(
            "agents", "dispatch_hold", "dispatch_hold INTEGER NOT NULL DEFAULT 0"
        )
        self.ensure_column(
            "agents", "dispatch_hold_reason", "dispatch_hold_reason TEXT"
        )
        self.ensure_column(
            "agents", "dispatch_hold_at", "dispatch_hold_at TEXT"
        )
        self.ensure_column(
            "agents",
            "consecutive_lease_expiries_no_telemetry",
            "consecutive_lease_expiries_no_telemetry INTEGER NOT NULL DEFAULT 0",
        )
        self.ensure_column(
            "agents",
            "last_control_stream_published_at",
            "last_control_stream_published_at TEXT",
        )
        self.ensure_column(
            "agents",
            "last_control_stream_consumed_at",
            "last_control_stream_consumed_at TEXT",
        )
        # reviews.findings: what the reviewer actually said. Additive and
        # idempotent, so an existing hub keeps every stored review and simply
        # starts recording judgement alongside the verdict.
        self.ensure_column(
            "reviews", "findings", "findings TEXT NOT NULL DEFAULT '{}'"
        )
        # fleet_release_admission_episodes: the base table is created by the
        # bundled schema (CREATE TABLE IF NOT EXISTS). These additive column
        # migrations upgrade any database that already has an earlier, partial
        # version of the table so SQLite<->Postgres parity holds. Each is
        # idempotent via ADD COLUMN IF NOT EXISTS.
        self.ensure_column(
            "fleet_release_admission_episodes", "project", "project TEXT"
        )
        self.ensure_column(
            "fleet_release_admission_episodes",
            "barrier_resource_digest",
            "barrier_resource_digest TEXT NOT NULL DEFAULT ''",
        )
        self.ensure_column(
            "fleet_release_admission_episodes",
            "owner_kind",
            "owner_kind TEXT NOT NULL DEFAULT 'publisher'",
        )
        self.ensure_column(
            "fleet_release_admission_episodes", "owner_id", "owner_id TEXT"
        )
        self.ensure_column(
            "fleet_release_admission_episodes",
            "waiter_kind",
            "waiter_kind TEXT NOT NULL DEFAULT 'epoch_opener'",
        )
        self.ensure_column(
            "fleet_release_admission_episodes", "waiter_id", "waiter_id TEXT"
        )
        self.ensure_column(
            "fleet_release_admission_episodes",
            "waiting_publishers",
            "waiting_publishers INTEGER NOT NULL DEFAULT 0",
        )
        self.ensure_column(
            "fleet_release_admission_episodes",
            "waiting_epoch_openers",
            "waiting_epoch_openers INTEGER NOT NULL DEFAULT 0",
        )
        self.ensure_column(
            "fleet_release_admission_episodes",
            "queue_depth",
            "queue_depth INTEGER NOT NULL DEFAULT 0",
        )
        self.ensure_column(
            "fleet_release_admission_episodes",
            "wait_started_at",
            "wait_started_at TEXT NOT NULL DEFAULT ''",
        )
        self.ensure_column(
            "fleet_release_admission_episodes",
            "wait_ended_at",
            "wait_ended_at TEXT",
        )
        self.ensure_column(
            "fleet_release_admission_episodes",
            "wait_seconds",
            "wait_seconds DOUBLE PRECISION",
        )
        self.ensure_column(
            "fleet_release_admission_episodes",
            "outcome",
            "outcome TEXT NOT NULL DEFAULT ''",
        )
        self.ensure_column(
            "fleet_release_admission_episodes",
            "metadata",
            "metadata TEXT NOT NULL DEFAULT '{}'",
        )
        from mac.task_dependencies import migrate_dependency_edges

        migrate_dependency_edges(self)

    def ensure_column(
        self, table: str, column: str, definition: str
    ) -> None:
        """Additive migration helper — matches SQLiteStore._ensure_column.

        ``definition`` includes the column name + type clause as in the
        existing call sites: ``ensure_column("agents", "role_id",
        "role_id TEXT")``. Postgres 9.6+ supports ``ADD COLUMN IF NOT
        EXISTS`` so the operation is naturally idempotent.
        """
        # `column` is unused server-side because `definition` already
        # carries it; keep the parameter so SQLiteStore and PostgresStore
        # have identical signatures.
        _ = column
        sql = "ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s" % (table, definition)
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
        except psycopg.Error as exc:
            raise StoreError(str(exc)) from exc

    def close(self) -> None:
        self._pool.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Result:
        translated = _translate_placeholders(sql)
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        translated, tuple(params) if params else None
                    )
                    rowcount, rows = _materialize(cur)
                    return _Result(rowcount, rows)
        except psycopg.Error as exc:
            raise StoreError(str(exc)) from exc

    def executemany(
        self, sql: str, params: Iterable[Sequence[Any]]
    ) -> _Result:
        translated = _translate_placeholders(sql)
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.executemany(translated, [tuple(p) for p in params])
                    return _Result(cur.rowcount or 0, [])
        except psycopg.Error as exc:
            raise StoreError(str(exc)) from exc

    def query_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> Optional[_Row]:
        return self.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> List[_Row]:
        return self.execute(sql, params).fetchall()

    def backend_identity(self) -> dict:
        """Identify this durable backend for read-only diagnostics.

        Returns the backend family (``"postgres"``) and a redacted DSN so an
        operator can confirm which authoritative cluster a ``mac admin diagnostics``
        report ran against without leaking credentials.
        """
        return {
            "backend": "postgres",
            "location": _redact_dsn(self.path),
            "in_memory": False,
        }

    @contextmanager
    def transaction(self) -> Iterator[StoreConnection]:
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    yield _Transaction(conn)
        except psycopg.Error as exc:
            raise StoreError(str(exc)) from exc
