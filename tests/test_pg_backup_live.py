"""Live-Postgres restore drill for mac.pg_backup.

Skipped unless MAC_TEST_PG_URL points at a writable database AND the pg client
binaries (pg_dump/pg_restore/psql) are on PATH. Proves the real end-to-end
contract the fake-runner unit tests model: a consistent pg_dump artifact
restores into a throwaway scratch database with matching representative row
counts, and the scratch database is dropped afterward.

Run with: MAC_TEST_PG_URL=postgresql://postgres:test@127.0.0.1:55432/mac \
          uv run --extra dev pytest -q -m postgres tests/test_pg_backup_live.py
"""
from __future__ import annotations

import os
import shutil
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.postgres

from mac import pg_backup  # noqa: E402


def _pg_url():
    url = os.environ.get("MAC_TEST_PG_URL", "").strip()
    if not url:
        pytest.skip("MAC_TEST_PG_URL not set")
    for binary in ("pg_dump", "pg_restore", "psql"):
        if shutil.which(binary) is None:
            pytest.skip("%s not on PATH" % binary)
    return url


@pytest.fixture()
def drill_database():
    """A database this drill owns outright.

    pg_dump dumps a whole database, and the shared test database now has
    per-test schemas being created and dropped by parallel workers. Dumping it
    races them: pg_dump resolves the schema list up front, then fails when one
    disappears underneath it ("schema ... does not exist"). Owning a database
    removes the race instead of papering over it with a retry.
    """
    import subprocess
    import uuid

    url = _pg_url()
    name = "mac_backup_drill_" + uuid.uuid4().hex[:12]
    admin = urlsplit(url)._replace(path="/postgres").geturl()

    def run(dsn, sql):
        return subprocess.run(
            ["psql", "--no-psqlrc", "--tuples-only", "--no-align",
             "--command", sql, dsn],
            capture_output=True, text=True,
        )

    created = run(admin, 'CREATE DATABASE "%s"' % name)
    if created.returncode != 0:
        pytest.skip("could not create drill database: %s" % created.stderr.strip()[:200])
    try:
        yield urlsplit(url)._replace(path="/" + name).geturl()
    finally:
        run(admin, 'DROP DATABASE IF EXISTS "%s"' % name)


def test_dump_and_restore_drill_round_trips(tmp_path, drill_database):
    url = drill_database
    # Seed a representative table on the live authority.
    import subprocess

    def psql(dsn, sql):
        return subprocess.run(
            ["psql", "--no-psqlrc", "--tuples-only", "--no-align",
             "--command", sql, dsn],
            capture_output=True, text=True,
        )

    psql(url, "CREATE TABLE IF NOT EXISTS pg_backup_probe (id INT PRIMARY KEY, v TEXT)")
    psql(url, "TRUNCATE pg_backup_probe")
    psql(url, "INSERT INTO pg_backup_probe VALUES (1,'a'),(2,'b'),(3,'c')")

    res = pg_backup.dump(
        url, tmp_path, verify=True, verify_tables=("pg_backup_probe",),
    )
    assert res.path.is_file()
    assert res.verified is True
    detail = res.verify_detail
    assert detail["ok"] is True
    assert detail["tables"]["restored"]["pg_backup_probe"] == 3
    assert detail["tables"]["live"]["pg_backup_probe"] == 3

    # scratch verification database was dropped
    scratch = detail["scratch_db"]
    admin = urlsplit(url)._replace(path="/postgres").geturl()
    out = psql(admin, "SELECT 1 FROM pg_database WHERE datname='%s'" % scratch)
    assert out.stdout.strip() == ""

    psql(url, "DROP TABLE IF EXISTS pg_backup_probe")
