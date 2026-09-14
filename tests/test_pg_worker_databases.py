"""Live contracts for xdist database isolation and ownership-safe cleanup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
import pytest

from tests.pg_worker_databases import WorkerDatabases, lock_key


@pytest.fixture
def databases():
    manager = WorkerDatabases(os.environ["MAC_TEST_PG_URL"])
    try:
        yield manager
    finally:
        manager.close()


def test_worker_dsn_supports_existing_schema_helpers(databases):
    from mac.test_support import create_schema

    dsn = databases.create()
    assert dsn.startswith("postgresql://")
    original = conninfo_to_dict(databases.dsn)
    resolved = conninfo_to_dict(dsn)
    assert {k: v for k, v in resolved.items() if k != "dbname"} == {
        k: v for k, v in original.items() if k != "dbname"
    }
    schema, scoped = create_schema(dsn)
    with psycopg.connect(scoped) as conn:
        assert conn.execute("SELECT current_schema()").fetchone()[0] == schema
    # create_schema registered this with the surrounding test's base-DB sweep;
    # this database belongs to this manager, which removes the whole database.
    from mac import test_support

    test_support._CREATED_SCHEMAS.remove(schema)


def test_database_isolation_preserves_same_database_lock_exclusion(databases):
    first, second = databases.create(), databases.create()
    with (
        psycopg.connect(first, autocommit=True) as a,
        psycopg.connect(first, autocommit=True) as peer,
        psycopg.connect(second, autocommit=True) as b,
    ):
        with a.transaction():
            a.execute("SELECT pg_advisory_xact_lock(hashtext('mac.schema_migrations'))")
            with peer.transaction():
                assert not peer.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext('mac.schema_migrations'))"
                ).fetchone()[0]
            with b.transaction():
                assert b.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext('mac.schema_migrations'))"
                ).fetchone()[0]
        with peer.transaction():
            assert peer.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext('mac.schema_migrations'))"
            ).fetchone()[0]


def test_reaper_preserves_live_owner_and_removes_abandoned_owned_database(databases):
    abandoned = WorkerDatabases(databases.dsn)
    name = conninfo_to_dict(abandoned.create())["dbname"]
    try:
        assert name not in databases.reap()
        abandoned.conn.close()  # Simulate controller termination, no teardown.
        assert name in databases.reap()
    finally:
        if not abandoned.conn.closed:
            abandoned.close()
        else:
            databases._drop(name)


def test_worker_lease_protects_database_after_controller_exit(databases):
    abandoned = WorkerDatabases(databases.dsn)
    name = conninfo_to_dict(abandoned.create())["dbname"]
    with psycopg.connect(databases.dsn, autocommit=True) as worker:
        worker.execute("SELECT pg_advisory_lock_shared(%s)", (lock_key(abandoned.run_id),))
        abandoned.conn.close()
        assert name not in databases.reap()
    assert name in databases.reap()


def test_reaper_does_not_drop_unmarked_or_mismatched_databases(databases):
    other = WorkerDatabases(databases.dsn)
    name = conninfo_to_dict(other.create())["dbname"]
    other.conn.close()
    try:
        for marker in [None, "not json", "[]", json.dumps({"schema": "wrong"})]:
            databases.conn.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(name), sql.Literal(marker)
                )
            )
            assert name not in databases.reap()
            assert databases.conn.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (name,)
            ).fetchone()
    finally:
        databases._drop(name)


def test_cleanup_never_forces_an_existing_client_out(databases):
    name = conninfo_to_dict(databases.create())["dbname"]
    dsn = databases.create()
    busy = conninfo_to_dict(dsn)["dbname"]
    with psycopg.connect(dsn) as client:
        with pytest.warns(UserWarning, match="ObjectInUse"):
            assert not databases._drop(busy)
        assert client.execute("SELECT 1").fetchone() == (1,)
    assert databases._drop(busy)
    assert databases._drop(name)


def test_real_xdist_workers_receive_distinct_databases_and_cleanup(tmp_path, databases):
    """Exercise actual controller hooks and collection-time DSN propagation."""
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["tests.pg_worker_databases"]\n')
    test = """import os, pathlib, psycopg
import pytest
@pytest.mark.parametrize("case", range(8))
def test_worker(case):
    with psycopg.connect(os.environ["MAC_TEST_PG_URL"]) as c:
        name = c.execute("select current_database()").fetchone()[0]
    pathlib.Path(os.environ["PROBE_DIR"], os.environ["PYTEST_XDIST_WORKER"]).write_text(name)
"""
    (tmp_path / "test_probe.py").write_text(test)
    env = {
        **os.environ,
        "PYTHONPATH": str(root) + os.pathsep + str(root / "src"),
        "PROBE_DIR": str(tmp_path),
    }
    for key in [
        "PYTEST_CURRENT_TEST",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
        "PYTEST_ADDOPTS",
    ]:
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n", "2", "--dist=load", "test_probe.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    names = {p.read_text() for p in tmp_path.glob("gw*")}
    assert len(names) == 2
    assert all(name.startswith("mac_pytest_") for name in names)
    for name in names:
        assert not databases.conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (name,)
        ).fetchone()
