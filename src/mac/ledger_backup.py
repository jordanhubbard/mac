"""Verified hub-ledger snapshots for warm-standby recovery.

The hub's SQLite ledger is the fleet's single durable authority; until now the
only backup was a manual ``sqlite3 .backup`` documented in the deployment
guide. This module makes ledger snapshots a scheduled, verified, shippable
operation:

- **Verified snapshot.** ``PRAGMA wal_checkpoint(FULL)`` + the SQLite online
  backup API + ``PRAGMA integrity_check`` (the same primitive the local-ledger
  migration uses), written atomically with a sha256 sidecar manifest, so a
  standby never restores a torn or corrupt copy.
- **Retention.** Timestamped snapshots under ``<out>/ledger/``, pruned to
  ``--keep-last`` (default 14).
- **Ship-to-standby hook.** ``MAC_LEDGER_BACKUP_SYNC_CMD`` (or ``--sync-cmd``)
  runs after each snapshot with ``MAC_LEDGER_SNAPSHOT_PATH`` /
  ``MAC_LEDGER_SNAPSHOT_SHA256`` / ``MAC_LEDGER_SNAPSHOT_MANIFEST`` in the
  environment — e.g. ``rsync -a "$MAC_LEDGER_SNAPSHOT_PATH"* standby:~/.mac/backups/ledger/``.
  Mirrors the journal service's ``MAC_JOURNAL_BACKUP_HOOK`` contract.

This is the SQLite half of the hub availability story (see
``docs/hub-availability.md``): snapshots bound the RPO for a standby promote.
Hubs that need a smaller RPO than a snapshot cadence should run the Postgres
backend (``MAC_DATABASE_URL``) and delegate replication to the database.

Promote safety: a restored standby becomes the fleet authority only through
the documented promote procedure (fence the old hub first — stop its service
or set ``MAC_CONTROL_PLANE_ROLE=client`` — then repoint ``hub_url``). Nothing
in this module ever starts a second live authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mac import mac_paths
from typing import List, Optional

SNAPSHOT_MANIFEST_SCHEMA = "mac.ledger_snapshot.v1"
SYNC_CMD_ENV = "MAC_LEDGER_BACKUP_SYNC_CMD"
DEFAULT_KEEP_LAST = 14


class LedgerBackupError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_backup(source_db: Path, destination: Path) -> None:
    """SQLite online backup + integrity check; deletes the copy on any failure."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    source = sqlite3.connect(str(source_db))
    target = sqlite3.connect(str(destination))
    try:
        source.execute("PRAGMA wal_checkpoint(FULL)")
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise LedgerBackupError("ledger snapshot failed integrity_check")
        target.commit()
    except Exception:
        target.close()
        source.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        target.close()
        source.close()
    destination.chmod(0o600)


def snapshot(
    db_path: Path,
    out_dir: Path,
    *,
    keep_last: int = DEFAULT_KEEP_LAST,
    sync_cmd: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Path:
    """Take one verified snapshot; prune old ones; run the ship hook.

    Returns the snapshot path. Raises ``LedgerBackupError`` on verification
    failure and propagates a non-zero ship-hook exit as an error so a broken
    standby sync is loud, not silent.
    """
    db_path = db_path.expanduser()
    if not db_path.is_file():
        raise LedgerBackupError("ledger database not found: %s" % db_path)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    ledger_dir = out_dir.expanduser() / "ledger"
    destination = ledger_dir / ("mac-%s.db" % stamp)
    _verified_backup(db_path, destination)
    digest = _sha256_file(destination)
    manifest = destination.with_suffix(".db.manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "schema": SNAPSHOT_MANIFEST_SCHEMA,
                "source_db": str(db_path),
                "snapshot": destination.name,
                "sha256": digest,
                "size_bytes": destination.stat().st_size,
                "created_at": stamp,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    prune(ledger_dir, keep_last=keep_last)
    hook = (sync_cmd if sync_cmd is not None else os.environ.get(SYNC_CMD_ENV) or "").strip()
    if hook:
        env = {
            **os.environ,
            "MAC_LEDGER_SNAPSHOT_PATH": str(destination),
            "MAC_LEDGER_SNAPSHOT_SHA256": digest,
            "MAC_LEDGER_SNAPSHOT_MANIFEST": str(manifest),
        }
        proc = subprocess.run(hook, shell=True, env=env)  # noqa: S602 - operator-supplied hook
        if proc.returncode != 0:
            raise LedgerBackupError(
                "ledger snapshot sync hook exited %d (snapshot kept at %s)"
                % (proc.returncode, destination)
            )
    return destination


def prune(ledger_dir: Path, *, keep_last: int = DEFAULT_KEEP_LAST) -> List[Path]:
    """Keep the newest ``keep_last`` snapshots (plus manifests); remove the rest."""
    if keep_last <= 0:
        return []
    snapshots = sorted(ledger_dir.glob("mac-*.db"))
    removed: List[Path] = []
    for stale in snapshots[:-keep_last] if len(snapshots) > keep_last else []:
        stale.unlink(missing_ok=True)
        manifest = stale.with_suffix(".db.manifest.json")
        manifest.unlink(missing_ok=True)
        removed.append(stale)
    return removed


def verify(snapshot_path: Path) -> bool:
    """Re-verify a snapshot against its manifest (standby-side check)."""
    manifest_path = snapshot_path.with_suffix(".db.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SNAPSHOT_MANIFEST_SCHEMA:
        raise LedgerBackupError("unknown snapshot manifest schema")
    if _sha256_file(snapshot_path) != str(manifest.get("sha256")):
        raise LedgerBackupError("snapshot sha256 does not match its manifest")
    conn = sqlite3.connect(str(snapshot_path))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if not integrity or integrity[0] != "ok":
        raise LedgerBackupError("snapshot failed integrity_check")
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mac.ledger_backup")
    parser.add_argument(
        "--db",
        default=str(mac_paths.ledger_db()),
    )
    parser.add_argument(
        "--out",
        default=os.environ.get("MAC_LEDGER_BACKUP_DIR")
        or str(mac_paths.backups_dir()),
    )
    parser.add_argument("--keep-last", type=int, default=DEFAULT_KEEP_LAST)
    parser.add_argument("--sync-cmd", default=None)
    parser.add_argument("--verify", metavar="SNAPSHOT", default=None)
    ns = parser.parse_args(argv)
    if ns.verify:
        verify(Path(ns.verify))
        print("snapshot verified: %s" % ns.verify)
        return 0
    path = snapshot(
        Path(ns.db), Path(ns.out), keep_last=ns.keep_last, sync_cmd=ns.sync_cmd
    )
    print("ledger snapshot written: %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
