"""Ephemeral Postgres control planes for the test suite.

The fleet runs on Postgres. For most of this project's life the test suite ran
on in-memory SQLite, which meant the engine under test was not the engine in
production -- and the gap was not theoretical: the NEEDS_INPUT rollout shipped
a task state that every SQLite test accepted and the live Postgres trigger
rejected, surfacing as a 500 on a production endpoint. Tests that agree with
each other but not with production are worse than no tests, because they
license the deploy.

This module gives each test its own PostgreSQL *schema* inside one shared
database. A schema is cheap (measured ~0.24s to create and apply the full DDL,
against ~0.15s for a fresh in-memory SQLite), it isolates completely, and it
keeps the connection pool warm across tests, which a database-per-test would
not.

Point the suite at a database with ``MAC_TEST_PG_URL``; ``scripts/start-test-
postgres.sh`` will start a container or use a local server and print the DSN.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, List, Optional

DEFAULT_TEST_DSN_ENV = "MAC_TEST_PG_URL"

# Schemas created since the last sweep. The pytest fixture in tests/conftest.py
# drops these after each test; without that they accumulate for the life of the
# database and a long run leaves thousands behind.
_CREATED_SCHEMAS: List[str] = []

# Open stores created for those schemas. Each holds a connection pool; dropping
# the schema without closing the pool leaks connections, and a parallel run
# exhausts Postgres' max_connections long before the suite finishes.
_OPEN_STORES: List[object] = []


class TestPostgresUnavailable(RuntimeError):
    """Raised when no test database is configured.

    Deliberately fatal rather than a skip: a silent skip is how a suite ends up
    green while covering nothing.
    """


def test_dsn() -> str:
    """Return the DSN for the shared test database, or explain how to get one."""
    dsn = os.environ.get(DEFAULT_TEST_DSN_ENV, "").strip()
    if dsn:
        return dsn
    raise TestPostgresUnavailable(
        "%s is unset, so there is no database to run against.\n"
        "Start one and export the DSN:\n"
        "    eval \"$(scripts/start-test-postgres.sh)\"\n"
        "or point at any existing server:\n"
        "    export %s=postgresql://user@127.0.0.1:5432/mac_test"
        % (DEFAULT_TEST_DSN_ENV, DEFAULT_TEST_DSN_ENV)
    )


def _scoped_dsn(dsn: str, schema: str) -> str:
    separator = "&" if "?" in dsn else "?"
    return "%s%soptions=-csearch_path%%3D%s" % (dsn, separator, schema)


def create_schema(dsn: Optional[str] = None) -> tuple[str, str]:
    """Create a fresh schema and return ``(schema_name, scoped_dsn)``."""
    import psycopg

    resolved = dsn or test_dsn()
    schema = "mac_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(resolved, autocommit=True) as conn:
        conn.execute('CREATE SCHEMA "%s"' % schema)
    _CREATED_SCHEMAS.append(schema)
    return schema, _scoped_dsn(resolved, schema)


def drop_created_schemas(dsn: Optional[str] = None) -> int:
    """Close pooled connections and drop every schema created since the last
    sweep. Returns how many schemas were dropped."""
    _DSN_BY_KEY.clear()
    stores, _OPEN_STORES[:] = list(_OPEN_STORES), []
    for store in stores:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - a wedged pool must not block the sweep
            pass
    if not _CREATED_SCHEMAS:
        return 0
    import psycopg

    resolved = dsn or os.environ.get(DEFAULT_TEST_DSN_ENV, "").strip()
    pending, _CREATED_SCHEMAS[:] = list(_CREATED_SCHEMAS), []
    if not resolved:
        return 0
    with psycopg.connect(resolved, autocommit=True) as conn:
        for schema in pending:
            conn.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)
    return len(pending)


_DSN_BY_KEY: dict = {}


def dsn_for(key: Any) -> str:
    """A stable DSN for ``key``, created once per test.

    CLI tests call their `_run` helper several times with the same tmp_path and
    expect state to persist between calls -- that used to be one SQLite file.
    The equivalent is one schema per test, so the DSN is memoized on the key
    rather than created per call. `drop_created_schemas` clears this cache
    along with the schemas.
    """
    # Idempotent: a helper that already resolved its DSN may hand it straight
    # back in. Creating a second schema for it would silently split a test's
    # writes from its reads, which is exactly the failure this helper exists to
    # prevent.
    text = str(key)
    if text.startswith(("postgres://", "postgresql://")):
        return text
    cached = _DSN_BY_KEY.get(text)
    if cached is None:
        cached = ephemeral_dsn()
        _DSN_BY_KEY[text] = cached
    return cached


def ephemeral_dsn(dsn: Optional[str] = None) -> str:
    """A DSN scoped to a fresh schema, for tests that open it more than once.

    `ephemeral_store` makes a new schema per call, which is right for a
    throwaway store but wrong for a test that closes a store and reopens the
    same database to prove durability. Those tests take a DSN once and open as
    many stores on it as they need -- the schema-per-test equivalent of
    reusing one SQLite file path.

    The DDL is applied here, so the DSN names a ready database. Callers that
    attach later (`mac --db`, the credential lifecycle) deliberately pass
    initialize_schema=False, and would otherwise find an empty schema.
    """
    store = ephemeral_store(dsn)
    return str(store.path)


def ephemeral_store(dsn: Optional[str] = None, *, pool_size: int = 2, swept: bool = True):
    """A `PostgresStore` on its own schema, with the full DDL applied.

    `swept=False` withholds the store from the per-test sweep in
    tests/conftest.py, which closes every open store and drops every created
    schema after EACH test. That sweep is right for the default per-test store,
    but it makes a longer-lived one impossible: a module-scoped schema is
    dropped and its pool closed after the first test in the module, and every
    later use fails on a closed pool.

    A caller that passes swept=False owns the cleanup and must call
    `drop_store` in its own teardown.
    """
    from mac.store_postgres import PostgresStore

    schema, scoped = create_schema(dsn)
    store = PostgresStore(scoped, pool_size=pool_size, min_size=1)
    store.initialize()
    store._mac_test_schema = schema
    if swept:
        _OPEN_STORES.append(store)
    else:
        # create_schema registered it; take it back out so the sweep leaves it.
        try:
            _CREATED_SCHEMAS.remove(schema)
        except ValueError:
            pass
    return store


def drop_store(store) -> None:
    """Close `store` and drop the schema it owns. The teardown counterpart to
    `ephemeral_store(swept=False)`."""
    schema = getattr(store, "_mac_test_schema", "")
    try:
        store.close()
    except Exception:  # noqa: BLE001 - a wedged pool must not block cleanup
        pass
    if not schema:
        return
    import psycopg

    resolved = os.environ.get(DEFAULT_TEST_DSN_ENV, "").strip()
    if not resolved:
        return
    try:
        with psycopg.connect(resolved, autocommit=True) as conn:
            conn.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


# Tables the packaged DDL leaves populated: the migration ledgers. A reset must
# preserve them, because "just initialized" includes their rows -- wiping them
# would make the next test look like a database whose migrations never ran.
_RESET_PRESERVED_TABLES = frozenset(
    {"task_dependency_migrations", "telemetry_data_migrations"}
)


def reset_store_data(store) -> bool:
    """Return `store` to its just-initialized state WITHOUT re-running the DDL.

    A per-test `ephemeral_store()` pays a real CREATE SCHEMA plus the packaged
    DDL -- 164 tables and 219 indexes, about 1.2s. Across the ~500 parametrized
    cases in the control-plane public contract that was 670 of the file's 745
    seconds: the gate's dominant cost was schema setup, not testing.

    Emptying the tables instead is the same isolation for a fraction of the
    cost, but only if it is done with DELETE:

        TRUNCATE tasks CASCADE          6.4s   (tasks is an FK target for dozens
                                                of tables; CASCADE pulls them all)
        TRUNCATE <all 161 tables>       2.0s
        probe + DELETE of dirty tables  0.03s

    So this probes for the tables that actually hold rows -- nearly always a
    handful -- and deletes only those. FK triggers are suppressed for the
    duration so the deletes need no dependency ordering.

    Returns False if the reset cannot be done safely (most likely because the
    connected role may not set session_replication_role). The caller must then
    fall back to a fresh schema: a partial reset would leak one test's rows
    into the next, which is worse than being slow.
    """
    tables = getattr(store, "_reset_tables", None)
    probe = getattr(store, "_reset_probe", None)
    sequences = getattr(store, "_reset_sequences", None)
    try:
        with store._pool.connection() as conn:
            with conn.cursor() as cur:
                if tables is None:
                    cur.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema() "
                        "AND table_type = 'BASE TABLE'"
                    )
                    tables = sorted(row[0] for row in cur.fetchall())
                    # One round trip that names every non-empty table. EXISTS
                    # stops at the first row, and an empty table is a zero-page
                    # scan, so this stays ~20ms across 161 tables.
                    probe = " UNION ALL ".join(
                        'SELECT %s AS t WHERE EXISTS (SELECT 1 FROM "%s")'
                        % (_sql_literal(name), name)
                        for name in tables
                    )
                    cur.execute(
                        "SELECT sequence_name FROM information_schema.sequences "
                        "WHERE sequence_schema = current_schema()"
                    )
                    sequences = sorted(row[0] for row in cur.fetchall())
                    store._reset_tables = tables
                    store._reset_probe = probe
                    store._reset_sequences = sequences

                # Suppressing FK triggers is what lets the deletes run in any
                # order. If the role cannot, say so rather than deleting a
                # subset and reporting success.
                cur.execute("SET session_replication_role = replica")

                cur.execute(probe)
                dirty = [
                    row[0]
                    for row in cur.fetchall()
                    if row[0] not in _RESET_PRESERVED_TABLES
                ]
                for name in dirty:
                    cur.execute('DELETE FROM "%s"' % name)

                # DELETE leaves sequences advanced where TRUNCATE ... RESTART
                # IDENTITY would have rewound them, so a reused schema would
                # hand out observability_events.sequence values that a fresh
                # one never would.
                for name in sequences:
                    cur.execute('ALTER SEQUENCE "%s" RESTART' % name)

                cur.execute("SET session_replication_role = DEFAULT")
            conn.commit()
    except Exception:
        return False
    return True


def _sql_literal(value: str) -> str:
    return "'%s'" % value.replace("'", "''")


def store_on(dsn: str, *, pool_size: int = 2, initialize: bool = False):
    """Open a store on an EXISTING scoped DSN, without creating a new schema.

    The counterpart to `dsn_for`: a test that drives the CLI with
    `--db dsn_for(tmp_path)` and also inspects the result directly must reach
    the same schema. `ephemeral_store` would make a fresh one and the test
    would look in the wrong place -- silently, since both succeed.
    """
    from mac.store_postgres import PostgresStore

    store = PostgresStore(dsn, pool_size=pool_size, min_size=1)
    if initialize:
        # Re-runs the DDL and the data migrations, which is what a test that
        # reopens a database to prove a migration fires actually needs.
        store.initialize()
    _OPEN_STORES.append(store)
    return store


def control_plane_on(dsn: str, **kwargs):
    """A `ControlPlane` on an existing scoped DSN."""
    from mac.services import ControlPlane

    kwargs.setdefault("secret_key", "test-key-with-enough-entropy-32+chars")
    return ControlPlane(store_on(dsn), **kwargs)


def table_names(store) -> set:
    """Every table in the store's own schema.

    The SQLite spelling (`SELECT name FROM sqlite_master WHERE type='table'`)
    has no Postgres equivalent, and tests that introspect the schema should not
    each reinvent the catalog query.
    """
    return {
        row["table_name"]
        for row in store.query_all(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        )
    }


def column_names(store, table: str) -> set:
    """Every column of ``table`` -- the portable `PRAGMA table_info`."""
    return {
        row["column_name"]
        for row in store.query_all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        )
    }


def index_names(store, table: str) -> set:
    """Every index on ``table`` -- the portable `PRAGMA index_list`."""
    return {
        row["indexname"]
        for row in store.query_all(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = ?",
            (table,),
        )
    }


def all_index_names(store) -> set:
    """Every index in the store's own schema -- the portable
    `SELECT name FROM sqlite_master WHERE type='index'`."""
    return {
        row["indexname"]
        for row in store.query_all(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
        )
    }


def foreign_keys(store, table: str) -> set:
    """(column, referenced_table) pairs -- the portable `PRAGMA foreign_key_list`."""
    return {
        (row["column_name"], row["foreign_table_name"])
        for row in store.query_all(
            """
            SELECT kcu.column_name, ccu.table_name AS foreign_table_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = current_schema()
              AND tc.table_name = ?
            """,
            (table,),
        )
    }


def drop_table_guards(store, table: str) -> list:
    """Drop every user trigger on ``table``. Test fixtures only.

    Some tests need to write a row the append-only/immutability guards forbid,
    in order to set up the very state a repair path is supposed to fix. SQLite
    spelled this as two DROP TRIGGER statements naming a `_immutable` and a
    `_no_delete` trigger; Postgres uses one trigger covering UPDATE OR DELETE,
    requires `ON <table>`, and names it differently. Discovering the triggers
    keeps the tests out of that bookkeeping.
    """
    dropped = []
    for row in store.query_all(
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE NOT t.tgisinternal AND c.relname = ? "
        "AND n.nspname = current_schema()",
        (table,),
    ):
        store.execute('DROP TRIGGER "%s" ON %s' % (row["tgname"], table))
        dropped.append(row["tgname"])
    return dropped


def ephemeral_control_plane(dsn: Optional[str] = None, **kwargs):
    """A `ControlPlane` on its own schema -- the test-suite replacement for
    the old in-memory SQLite control plane."""
    from mac.services import ControlPlane

    kwargs.setdefault("secret_key", "test-key-with-enough-entropy-32+chars")
    return ControlPlane(ephemeral_store(dsn), **kwargs)
