"""Verified ledger snapshots (mac.ledger_backup) — the SQLite warm-standby half."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from mac import ledger_backup


@pytest.fixture()
def ledger(tmp_path):
    db = tmp_path / "mac.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
    conn.executemany(
        "INSERT INTO tasks (id, title) VALUES (?, ?)",
        [("task_%d" % i, "row %d" % i) for i in range(50)],
    )
    conn.commit()
    conn.close()
    return db


def test_snapshot_is_verified_and_restorable(ledger, tmp_path):
    out = tmp_path / "backups"
    path = ledger_backup.snapshot(ledger, out)
    assert path.is_file()
    manifest = json.loads(path.with_suffix(".db.manifest.json").read_text())
    assert manifest["schema"] == ledger_backup.SNAPSHOT_MANIFEST_SCHEMA
    assert ledger_backup.verify(path) is True
    # The snapshot is a complete, openable ledger copy.
    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 50
    conn.close()


def test_snapshot_prunes_to_keep_last(ledger, tmp_path):
    out = tmp_path / "backups"
    for i in range(5):
        ledger_backup.snapshot(
            ledger,
            out,
            keep_last=3,
            now=datetime(2026, 7, 2, 0, 0, i, tzinfo=timezone.utc),
        )
    snapshots = sorted((out / "ledger").glob("mac-*.db"))
    assert len(snapshots) == 3
    # Newest survive.
    assert snapshots[-1].name.endswith("000004Z.db")
    # Manifests pruned alongside.
    assert len(list((out / "ledger").glob("*.manifest.json"))) == 3


def test_sync_hook_receives_snapshot_env_and_failures_are_loud(ledger, tmp_path):
    out = tmp_path / "backups"
    marker = tmp_path / "shipped.txt"
    ledger_backup.snapshot(
        ledger,
        out,
        sync_cmd=(
            'printf "%s %s" "$MAC_LEDGER_SNAPSHOT_PATH" "$MAC_LEDGER_SNAPSHOT_SHA256" > '
            + str(marker)
        ),
    )
    shipped_path, shipped_sha = marker.read_text().split()
    assert shipped_path.endswith(".db")
    assert len(shipped_sha) == 64

    with pytest.raises(ledger_backup.LedgerBackupError, match="sync hook exited"):
        ledger_backup.snapshot(ledger, out, sync_cmd="exit 3")


def test_verify_rejects_tampered_snapshot(ledger, tmp_path):
    out = tmp_path / "backups"
    path = ledger_backup.snapshot(ledger, out)
    with path.open("r+b") as handle:
        handle.seek(100)
        handle.write(b"\x00\x00\x00\x00")
    with pytest.raises(ledger_backup.LedgerBackupError):
        ledger_backup.verify(path)


def test_missing_db_fails_closed(tmp_path):
    with pytest.raises(ledger_backup.LedgerBackupError, match="not found"):
        ledger_backup.snapshot(tmp_path / "absent.db", tmp_path / "backups")
