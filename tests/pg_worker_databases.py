"""Database-per-xdist-worker isolation; production migrations are unchanged.

The controller and workers hold shared advisory leases in the original test
DB. A later run may reap marked leftovers only after acquiring an exclusive
lease. No unmarked database is eligible and DROP never forces clients out.
"""

from __future__ import annotations

import json
import os
import re
import uuid
import warnings
from urllib.parse import urlencode

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
import pytest

PREFIX = "mac_pytest_"
MARKER = "mac.pytest.database.v1"
NAME = re.compile(r"mac_pytest_([0-9a-f]{32})_([0-9a-f]{8})\Z")


def lock_key(run_id: str) -> int:
    return int.from_bytes(bytes.fromhex(run_id)[:8], "big", signed=True)


class WorkerDatabases:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.run_id = uuid.uuid4().hex
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.base_oid, self.role = self.conn.execute(
            "SELECT oid, current_user FROM pg_database WHERE datname=current_database()"
        ).fetchone()
        self.names: set[str] = set()
        self.conn.execute("SELECT pg_advisory_lock_shared(%s)", (lock_key(self.run_id),))

    def create(self) -> str:
        name = PREFIX + self.run_id + "_" + uuid.uuid4().hex[:8]
        self.conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
        )
        self.names.add(name)
        marker = json.dumps({"schema": MARKER, "run_id": self.run_id, "base_oid": self.base_oid})
        self.conn.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(name), sql.Literal(marker)
            )
        )
        params = conninfo_to_dict(self.dsn)
        params.pop("dbname", None)
        # Test schema helpers append URL query options, so retain URI form.
        # libpq accepts all connection parameters in the query, including Unix
        # sockets and multi-host settings, without reconstructing an authority.
        return "postgresql:///" + name + "?" + urlencode(params)

    def _drop(self, name: str) -> bool:
        try:
            # No FORCE: an orphaned client is evidence to retain, not terminate.
            self.conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        except psycopg.Error as exc:
            warnings.warn(f"Retained pytest database {name}: {type(exc).__name__}", stacklevel=2)
            return False
        return True

    def reap(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT datname, shobj_description(oid, 'pg_database') FROM pg_database "
            "WHERE datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user) "
            "AND starts_with(datname, %s)",
            (PREFIX,),
        ).fetchall()
        removed = []
        for name, comment in rows:
            match = NAME.fullmatch(name)
            if not match or not comment:
                continue
            try:
                marker = json.loads(comment)
            except (ValueError, TypeError):
                continue
            if (
                not isinstance(marker, dict)
                or marker != {"schema": MARKER, "run_id": match[1], "base_oid": self.base_oid}
                or match[1] == self.run_id
            ):
                continue
            key = lock_key(match[1])
            acquired = self.conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
            if not acquired:
                continue
            try:
                if self._drop(name):
                    removed.append(name)
            finally:
                self.conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        return removed

    def close(self) -> None:
        try:
            for name in sorted(self.names):
                self._drop(name)
        finally:
            self.conn.close()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    worker = getattr(config, "workerinput", {})
    settings = worker.get("mac_postgres")
    if settings:
        # Hold the parent's run lease even if its controller crashes while we
        # are still executing. Acquire before collecting any repository tests.
        conn = psycopg.connect(settings["base_dsn"], autocommit=True)
        conn.execute("SELECT pg_advisory_lock_shared(%s)", (lock_key(settings["run_id"]),))
        config._mac_postgres_worker_lease = conn
        config._mac_postgres_original_dsn = os.environ.get("MAC_TEST_PG_URL")
        os.environ["MAC_TEST_PG_URL"] = settings["dsn"]


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    config = node.config
    manager = getattr(config, "_mac_postgres_databases", None)
    try:
        if manager is None:
            manager = WorkerDatabases(os.environ["MAC_TEST_PG_URL"])
            config._mac_postgres_databases = manager
            manager.reap()
        dsn = manager.create()
    except (psycopg.Error, KeyError) as exc:
        raise pytest.UsageError(
            "Cannot provision isolated pytest worker databases. MAC_TEST_PG_URL must "
            "use a test-server role with CREATEDB permission; " + type(exc).__name__
        ) from None
    node.workerinput["mac_postgres"] = {
        "base_dsn": manager.dsn,
        "dsn": dsn,
        "run_id": manager.run_id,
    }


def pytest_unconfigure(config):
    conn = getattr(config, "_mac_postgres_worker_lease", None)
    if conn is not None:
        conn.close()
        original = config._mac_postgres_original_dsn
        if original is None:
            os.environ.pop("MAC_TEST_PG_URL", None)
        else:
            os.environ["MAC_TEST_PG_URL"] = original
    manager = getattr(config, "_mac_postgres_databases", None)
    if manager is not None:
        manager.close()
