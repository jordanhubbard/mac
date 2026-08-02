"""Verified PostgreSQL authority backups (mac.pg_backup) — the Postgres half.

These pin the backup contract without a live cluster by injecting a fake
subprocess runner that models pg_dump/pg_restore/psql: consistent dump,
owner-only artifacts (0600 in a 0700 dir), sha256 manifest, retention, the
restore-to-scratch drill (schema + representative row counts), a loud failure
path, and the invariant that a Postgres backup NEVER falls back to SQLite.
"""
from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mac import pg_backup

DSN = "postgresql://mac:secret@127.0.0.1:5432/mac"


class FakePg:
    """Models the pg client binaries pg_backup shells out to.

    - ``pg_dump`` writes a small artifact file (so size/sha256 are real).
    - ``psql`` answers CREATE/DROP DATABASE and ``SELECT COUNT(*)`` from a
      configurable per-table row-count map.
    - ``pg_restore`` is a no-op success.
    """

    def __init__(self, counts=None, dump_rc=0, restore_rc=0, count_rc=0):
        self.counts = counts if counts is not None else {"tasks": 3, "agents": 2, "events": 5}
        self.dump_rc = dump_rc
        self.restore_rc = restore_rc
        self.count_rc = count_rc
        self.calls = []

    def __call__(self, argv, env):
        argv = list(argv)
        self.calls.append(argv)
        prog = Path(argv[0]).name

        class R:
            def __init__(self, rc=0, out="", err=""):
                self.returncode = rc
                self.stdout = out
                self.stderr = err

        if prog == "pg_dump":
            target = next(a.split("=", 1)[1] for a in argv if a.startswith("--file="))
            if self.dump_rc == 0:
                Path(target).write_bytes(b"PGDMP-fake-custom-archive-bytes")
            return R(self.dump_rc, err="" if self.dump_rc == 0 else "dump boom")
        if prog == "pg_restore":
            return R(self.restore_rc, err="" if self.restore_rc == 0 else "restore boom")
        if prog == "psql":
            sql = argv[argv.index("--command") + 1]
            if sql.upper().startswith(("CREATE DATABASE", "DROP DATABASE", "SELECT PG_TERMINATE")):
                return R(0)
            if sql.upper().startswith("SELECT COUNT(*) FROM "):
                table = sql.split("FROM", 1)[1].strip()
                if self.count_rc != 0:
                    return R(self.count_rc, err="no such table")
                if table not in self.counts:
                    return R(1, err='relation "%s" does not exist' % table)
                return R(0, out="%d\n" % self.counts[table])
            return R(0)
        raise AssertionError("unexpected program %r" % prog)

    def names(self):
        return [Path(c[0]).name for c in self.calls]


def _now():
    return datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_dump_is_verified_owner_only_and_manifested(tmp_path):
    out = tmp_path / "backups"
    fake = FakePg()
    res = pg_backup.dump(DSN, out, now=_now(), runner=fake)

    assert res.path.is_file()
    assert res.path.parent == out / "postgres"
    # owner-only artifact + directory
    assert stat.S_IMODE(res.path.stat().st_mode) == 0o600
    assert stat.S_IMODE((out / "postgres").stat().st_mode) == 0o700
    # sha256 manifest sidecar
    manifest = json.loads(res.manifest.read_text())
    assert manifest["schema"] == pg_backup.BACKUP_MANIFEST_SCHEMA
    assert manifest["sha256"] == res.sha256
    assert manifest["restore_verified"] is True
    assert res.verified is True
    # the restore drill actually ran pg_restore into a scratch db
    assert "pg_restore" in fake.names()
    assert "pg_dump" in fake.names()


def test_restore_drill_fails_when_counts_diverge(tmp_path):
    # scratch restore reports a different row count than the live authority
    class Divergent(FakePg):
        def __call__(self, argv, env):
            argv = list(argv)
            prog = Path(argv[0]).name
            if prog == "psql":
                sql = argv[argv.index("--command") + 1]
                if sql.strip().startswith("SELECT COUNT(*) FROM tasks"):
                    dsn = argv[-1]
                    # scratch db has fewer rows than live -> torn/partial restore
                    out = "1\n" if "restore_verify" in dsn else "3\n"

                    class R:
                        returncode = 0
                        stdout = out
                        stderr = ""

                    self.calls.append(argv)
                    return R()
            return super().__call__(argv, env)

    with pytest.raises(pg_backup.PgBackupError, match="row count"):
        pg_backup.dump(DSN, tmp_path / "b", now=_now(), runner=Divergent())


def test_restore_drill_fails_when_table_absent(tmp_path):
    fake = FakePg(counts={"agents": 2, "events": 5})  # tasks missing from restore
    with pytest.raises(pg_backup.PgBackupError, match="absent"):
        pg_backup.dump(DSN, tmp_path / "b", now=_now(), runner=fake)


def test_dump_failure_is_loud_and_leaves_no_artifact(tmp_path):
    out = tmp_path / "b"
    with pytest.raises(pg_backup.PgBackupError, match="pg_dump exited"):
        pg_backup.dump(DSN, out, now=_now(), runner=FakePg(dump_rc=2))
    assert not list((out / "postgres").glob("*.dump")) if (out / "postgres").exists() else True


def test_never_falls_back_to_sqlite_on_non_pg_dsn(tmp_path):
    for bad in ["", "sqlite:///x.db", "/home/x/.mac/mac.db", "mysql://y"]:
        with pytest.raises(pg_backup.PgBackupError):
            pg_backup.dump(bad, tmp_path / "b", runner=FakePg())


def test_prune_keeps_last_n(tmp_path):
    out = tmp_path / "b"
    for i in range(5):
        pg_backup.dump(
            DSN, out, keep_last=3, verify=False,
            now=datetime(2026, 7, 28, 0, 0, i, tzinfo=timezone.utc),
            runner=FakePg(),
        )
    dumps = sorted((out / "postgres").glob("mac-*.dump"))
    assert len(dumps) == 3
    assert dumps[-1].name.endswith("000004Z.dump")
    assert len(list((out / "postgres").glob("*.manifest.json"))) == 3


def test_verify_manifest_detects_tampering(tmp_path):
    out = tmp_path / "b"
    res = pg_backup.dump(DSN, out, verify=False, now=_now(), runner=FakePg())
    assert pg_backup.verify_manifest(res.path) is True
    with res.path.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"\x00\x00")
    with pytest.raises(pg_backup.PgBackupError, match="sha256"):
        pg_backup.verify_manifest(res.path)


def test_sync_hook_receives_env_and_failures_are_loud(tmp_path):
    out = tmp_path / "b"
    marker = tmp_path / "shipped.txt"
    pg_backup.dump(
        DSN, out, verify=False, now=_now(), runner=FakePg(),
        sync_cmd='printf "%s %s" "$MAC_PG_BACKUP_PATH" "$MAC_PG_BACKUP_SHA256" > '
        + str(marker),
    )
    shipped_path, shipped_sha = marker.read_text().split()
    assert shipped_path.endswith(".dump")
    assert len(shipped_sha) == 64

    with pytest.raises(pg_backup.PgBackupError, match="sync hook exited"):
        pg_backup.dump(DSN, out, verify=False, now=_now(), runner=FakePg(),
                       sync_cmd="exit 4")


# --- local socket DSN ------------------------------------------------------
#
# Every test above uses a host-form DSN, which is why the production failure
# below was invisible: the bug only appears when the authority is EMPTY.

SOCKET_DSN = "postgresql:///mac"


def test_admin_dsn_preserves_an_empty_authority():
    """``postgresql:///mac`` must not collapse to ``postgresql:/postgres``.

    urlsplit/urlunsplit drops the ``//`` when netloc is empty, and libpq then
    reads the result as a database *named* ``postgresql:/postgres``.
    """
    assert pg_backup._admin_dsn(SOCKET_DSN, "postgres") == "postgresql:///postgres"
    assert pg_backup._admin_dsn(SOCKET_DSN, "scratch_db") == "postgresql:///scratch_db"
    # Host forms keep working, including credentials and port.
    assert (
        pg_backup._admin_dsn("postgresql://u:pw@host:5432/mac", "postgres")
        == "postgresql://u:pw@host:5432/postgres"
    )


def test_socket_dsn_backup_verifies_and_manifests(tmp_path):
    """A socket-DSN hub must produce a VERIFIED, manifested backup.

    On the production hub (``MAC_DATABASE_URL=postgresql:///mac``) the dump
    succeeded and restore verification then failed on every scheduled run, so
    no manifest was written and nothing was shipped off the box. The backup
    looked present on disk and was absent where it mattered.
    """
    out = tmp_path / "backups"
    fake = FakePg()
    res = pg_backup.dump(SOCKET_DSN, out, now=_now(), runner=fake)

    assert res.verified is True
    manifest = json.loads(res.manifest.read_text())
    assert manifest["restore_verified"] is True

    # Every psql target the drill used must still address the socket server.
    psql_targets = [c[-1] for c in fake.calls if Path(c[0]).name == "psql"]
    assert psql_targets, "the restore drill must have run psql"
    for target in psql_targets:
        assert target.startswith("postgresql:///"), target
        assert not target.startswith("postgresql:/postgres"), target


# --- churn tolerance -------------------------------------------------------
#
# A dump is a point-in-time snapshot and the live table keeps moving while the
# drill runs, so exact equality compares a photograph to a moving target.


def test_counts_consistent_absorbs_ordinary_churn():
    tol = pg_backup.DEFAULT_VERIFY_TOLERANCE
    # The production failure: 1,311,209 live vs 1,311,496 restored -- 0.02%.
    assert pg_backup._counts_are_consistent(1311209, 1311496, tol)
    # Churn in either direction; retention pruning moves the live count down.
    assert pg_backup._counts_are_consistent(1311496, 1311209, tol)
    assert pg_backup._counts_are_consistent(100, 100, tol)


def test_counts_consistent_still_catches_the_failures_the_drill_exists_for():
    tol = pg_backup.DEFAULT_VERIFY_TOLERANCE
    # Schema-only or truncated dump: live has rows, restore has none. Always a
    # failure regardless of tolerance.
    assert not pg_backup._counts_are_consistent(1311209, 0, tol)
    assert not pg_backup._counts_are_consistent(1, 0, tol)
    # Partial restore, order-of-magnitude short.
    assert not pg_backup._counts_are_consistent(1311209, 700000, tol)
    assert not pg_backup._counts_are_consistent(3, 1, tol)
    # A live table that is genuinely empty must restore empty.
    assert pg_backup._counts_are_consistent(0, 0, tol)
    assert not pg_backup._counts_are_consistent(0, 5, tol)


def test_verify_tolerance_is_configurable_and_clamped():
    assert pg_backup._verify_tolerance({}) == pg_backup.DEFAULT_VERIFY_TOLERANCE
    assert pg_backup._verify_tolerance({pg_backup.VERIFY_TOLERANCE_ENV: "0.2"}) == 0.2
    # Garbage falls back rather than disabling the gate.
    assert (
        pg_backup._verify_tolerance({pg_backup.VERIFY_TOLERANCE_ENV: "nonsense"})
        == pg_backup.DEFAULT_VERIFY_TOLERANCE
    )
    # Clamped into [0, 1] so a typo cannot make the check meaningless.
    assert pg_backup._verify_tolerance({pg_backup.VERIFY_TOLERANCE_ENV: "-1"}) == 0.0
    assert pg_backup._verify_tolerance({pg_backup.VERIFY_TOLERANCE_ENV: "99"}) == 1.0


def test_dump_succeeds_while_the_live_table_is_churning(tmp_path):
    """End to end: a busy hub must still produce a verified, manifested backup."""

    class Churning(FakePg):
        def __call__(self, argv, env):
            argv = list(argv)
            if Path(argv[0]).name == "psql":
                sql = argv[argv.index("--command") + 1]
                if sql.strip().startswith("SELECT COUNT(*) FROM events"):
                    dsn = argv[-1]
                    # Live moved on by a few rows between dump and count.
                    out = "1311496\n" if "restore_verify" in dsn else "1311209\n"

                    class R:
                        returncode = 0
                        stdout = out
                        stderr = ""

                    self.calls.append(argv)
                    return R()
            return super().__call__(argv, env)

    res = pg_backup.dump(DSN, tmp_path / "b", now=_now(), runner=Churning())
    assert res.verified is True
    assert json.loads(res.manifest.read_text())["restore_verified"] is True
