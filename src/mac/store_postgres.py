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
    """Buffered query result; matches the slice of cursor API callers use."""

    __slots__ = ("rowcount", "_rows")

    def __init__(self, rowcount: int, rows: List[_Row]) -> None:
        self.rowcount = rowcount
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


class PostgresStore:
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
        )

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

    @contextmanager
    def transaction(self) -> Iterator[StoreConnection]:
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    yield _Transaction(conn)
        except psycopg.Error as exc:
            raise StoreError(str(exc)) from exc
