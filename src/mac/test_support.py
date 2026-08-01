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
from typing import List, Optional

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


def ephemeral_store(dsn: Optional[str] = None, *, pool_size: int = 2):
    """A `PostgresStore` on its own schema, with the full DDL applied."""
    from mac.store_postgres import PostgresStore

    _, scoped = create_schema(dsn)
    store = PostgresStore(scoped, pool_size=pool_size, min_size=1)
    store.initialize()
    _OPEN_STORES.append(store)
    return store


def ephemeral_control_plane(dsn: Optional[str] = None, **kwargs):
    """A `ControlPlane` on its own schema -- the test-suite replacement for
    the old in-memory SQLite control plane."""
    from mac.services import ControlPlane

    kwargs.setdefault("secret_key", "test-key-with-enough-entropy-32+chars")
    return ControlPlane(ephemeral_store(dsn), **kwargs)
