"""The hub ledger must be backed up on a schedule so a lost hub node is not a
lost fleet (the SPOF that surfaced when the hub node dropped off the network).

These pin the scheduler wiring: default-on for hubs, off for clients, a verified
snapshot each run, off-box ship hook invoked, and a backup failure that is loud
but never crashes the daemon.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mac.ledger_backup_scheduler import LedgerBackupConfig, LedgerBackupScheduler


@pytest.fixture
def ledger_db(tmp_path):
    db = tmp_path / "mac.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO tasks VALUES ('t1', 'hello')")
    conn.commit()
    conn.close()
    return db


class RecordingCP:
    def __init__(self):
        self.logs = []
        self.notifications = []

    def record_log(self, event, **kw):
        self.logs.append((event, kw))

    def record_notification(self, name, title, body, **kw):
        self.notifications.append((name, title))


def test_default_on_for_hub_off_for_client():
    assert LedgerBackupConfig.from_env({}).enabled is True
    assert LedgerBackupConfig.from_env({"MAC_CONTROL_PLANE_ROLE": "client"}).enabled is False
    assert LedgerBackupConfig.from_env({"MAC_LEDGER_BACKUP_ENABLED": "0"}).enabled is False


def test_config_from_env_paths_and_interval(tmp_path):
    cfg = LedgerBackupConfig.from_env({
        "MAC_LEDGER_BACKUP_DB": str(tmp_path / "x.db"),
        "MAC_LEDGER_BACKUP_DIR": str(tmp_path / "backups"),
        "MAC_LEDGER_BACKUP_INTERVAL_SECONDS": "300",
        "MAC_LEDGER_BACKUP_KEEP_LAST": "3",
        "MAC_LEDGER_BACKUP_SYNC_CMD": "echo ship",
    })
    assert cfg.interval_seconds == 300
    assert cfg.keep_last == 3
    assert cfg.sync_cmd == "echo ship"


def test_run_once_produces_verified_snapshot(ledger_db, tmp_path):
    cp = RecordingCP()
    out = tmp_path / "backups"
    cfg = LedgerBackupConfig(enabled=True, db_path=str(ledger_db), out_dir=str(out),
                             interval_seconds=60, keep_last=5)
    sched = LedgerBackupScheduler(cp, cfg)
    res = sched.run_once(trigger="test")
    assert res["status"] == "ok"
    snap = Path(res["snapshot"])
    assert snap.exists() and snap.parent == out / "ledger"
    # manifest sidecar with sha256 was written
    manifest = snap.with_suffix(".db.manifest.json")
    assert manifest.exists()
    # the snapshot is a real, queryable copy of the ledger
    conn = sqlite3.connect(str(snap))
    assert conn.execute("SELECT title FROM tasks WHERE id='t1'").fetchone()[0] == "hello"
    conn.close()
    assert any(e == "ledger.backup.run" for e, _ in cp.logs)


def test_ship_hook_runs_with_snapshot_env(ledger_db, tmp_path):
    receipt = tmp_path / "shipped.txt"
    cfg = LedgerBackupConfig(
        enabled=True, db_path=str(ledger_db), out_dir=str(tmp_path / "b"),
        keep_last=5,
        sync_cmd='echo "$MAC_LEDGER_SNAPSHOT_SHA256" > "%s"' % receipt,
    )
    res = LedgerBackupScheduler(RecordingCP(), cfg).run_once()
    assert res["status"] == "ok" and res["shipped"] is True
    assert receipt.read_text().strip()  # sha256 was exported to the hook


def test_keep_last_prunes_old_snapshots(ledger_db, tmp_path):
    out = tmp_path / "b"
    cfg = LedgerBackupConfig(enabled=True, db_path=str(ledger_db), out_dir=str(out), keep_last=2)
    sched = LedgerBackupScheduler(RecordingCP(), cfg)
    import mac.ledger_backup as lb
    from datetime import datetime, timezone
    # distinct timestamps so snapshots don't collide, and pruning is observable
    for i in range(4):
        lb.snapshot(Path(cfg.db_path), Path(cfg.out_dir), keep_last=2,
                    now=datetime(2026, 1, 1, 0, i, tzinfo=timezone.utc))
    snaps = sorted((out / "ledger").glob("mac-*.db"))
    assert len(snaps) == 2  # only the newest 2 survive


def test_backup_failure_is_loud_but_does_not_raise(tmp_path):
    cp = RecordingCP()
    cfg = LedgerBackupConfig(enabled=True, db_path=str(tmp_path / "missing.db"),
                             out_dir=str(tmp_path / "b"))
    sched = LedgerBackupScheduler(cp, cfg)
    res = sched.run_once()   # must not raise even though the DB is missing
    assert res["status"] == "error"
    assert any(e == "ledger.backup.failed" for e, _ in cp.logs)
    assert cp.notifications  # operator was paged


def test_disabled_scheduler_start_is_noop():
    cfg = LedgerBackupConfig(enabled=False)
    assert LedgerBackupScheduler(None, cfg).start() is False
