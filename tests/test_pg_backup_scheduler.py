"""The hub PostgreSQL authority must be backed up on a schedule, restore-verified,
and its failures surfaced — never silently downgraded to SQLite.

These pin the scheduler wiring: default-on only when the authority is Postgres,
off for SQLite hubs and clients, a verified backup each run, the ship hook, the
periodic restore drill cadence, and a failure that is loud but never crashes the
daemon.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mac import pg_backup
from mac.pg_backup_scheduler import PgBackupConfig, PgBackupScheduler

PG = "postgresql://mac:secret@127.0.0.1:5432/mac"


class RecordingCP:
    def __init__(self):
        self.logs = []
        self.notifications = []

    def record_log(self, event, **kw):
        self.logs.append((event, kw))

    def record_notification(self, name, title, body, **kw):
        self.notifications.append((name, title, body))


def test_default_on_only_for_postgres_hub():
    assert PgBackupConfig.from_env({"MAC_DATABASE_URL": PG}).enabled is True
    # SQLite hub: no-op, the ledger scheduler owns that tier
    assert PgBackupConfig.from_env({"MAC_DB": "/x/mac.db"}).enabled is False
    assert PgBackupConfig.from_env({}).enabled is False
    # client role never backs up
    assert PgBackupConfig.from_env(
        {"MAC_DATABASE_URL": PG, "MAC_CONTROL_PLANE_ROLE": "client"}
    ).enabled is False
    # explicit off
    assert PgBackupConfig.from_env(
        {"MAC_DATABASE_URL": PG, "MAC_PG_BACKUP_ENABLED": "0"}
    ).enabled is False


def test_config_from_env_paths_interval_and_verify_every(tmp_path):
    cfg = PgBackupConfig.from_env({
        "MAC_DATABASE_URL": PG,
        "MAC_PG_BACKUP_DIR": str(tmp_path / "backups"),
        "MAC_PG_BACKUP_INTERVAL_SECONDS": "1800",
        "MAC_PG_BACKUP_KEEP_LAST": "7",
        "MAC_PG_BACKUP_VERIFY_EVERY": "4",
        "MAC_PG_BACKUP_SYNC_CMD": "echo ship",
    })
    assert cfg.interval_seconds == 1800
    assert cfg.keep_last == 7
    assert cfg.verify_every == 4
    assert cfg.sync_cmd == "echo ship"
    assert cfg.dsn == PG


def test_run_once_produces_verified_backup(monkeypatch, tmp_path):
    captured = {}

    def fake_dump(dsn, out_dir, *, keep_last, verify, sync_cmd):
        captured["verify"] = verify
        artifact = Path(out_dir) / "postgres" / "mac-x.dump"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"x")
        return pg_backup.BackupResult(
            path=artifact, manifest=artifact.with_suffix(".dump.manifest.json"),
            sha256="a" * 64, size_bytes=1, created_at="x", verified=True,
            verify_detail={"ok": True},
        )

    monkeypatch.setattr(pg_backup, "dump", fake_dump)
    cp = RecordingCP()
    cfg = PgBackupConfig(enabled=True, dsn=PG, out_dir=str(tmp_path / "b"),
                         interval_seconds=60, keep_last=5, verify_every=1)
    res = PgBackupScheduler(cp, cfg).run_once(trigger="test")
    assert res["status"] == "ok"
    assert res["restore_verified"] is True
    assert res["verify_performed"] is True
    assert captured["verify"] is True
    assert any(e == "pg.backup.run" for e, _ in cp.logs)


def test_verify_every_controls_restore_drill_cadence(monkeypatch, tmp_path):
    verifies = []

    def fake_dump(dsn, out_dir, *, keep_last, verify, sync_cmd):
        verifies.append(verify)
        artifact = Path(out_dir) / "postgres" / "mac-x.dump"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"x")
        return pg_backup.BackupResult(
            path=artifact, manifest=artifact, sha256="a" * 64, size_bytes=1,
            created_at="x", verified=verify, verify_detail={},
        )

    monkeypatch.setattr(pg_backup, "dump", fake_dump)
    cfg = PgBackupConfig(enabled=True, dsn=PG, out_dir=str(tmp_path / "b"),
                         keep_last=5, verify_every=3)
    sched = PgBackupScheduler(RecordingCP(), cfg)
    for _ in range(3):
        sched.run_once()
    # only every 3rd run performs the (costly) restore-to-scratch drill
    assert verifies == [False, False, True]


def test_backup_failure_is_loud_but_never_falls_back(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise pg_backup.PgBackupError("cluster unreachable")

    monkeypatch.setattr(pg_backup, "dump", boom)
    cp = RecordingCP()
    cfg = PgBackupConfig(enabled=True, dsn=PG, out_dir=str(tmp_path / "b"))
    res = PgBackupScheduler(cp, cfg).run_once()
    assert res["status"] == "error"
    assert any(e == "pg.backup.failed" for e, _ in cp.logs)
    # operator is paged and the message states there is NO SQLite fallback
    assert cp.notifications
    _, _, body = cp.notifications[0]
    assert "NO SQLite fallback" in body


def test_disabled_scheduler_start_is_noop():
    assert PgBackupScheduler(None, PgBackupConfig(enabled=False)).start() is False
