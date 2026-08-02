"""Scheduled hub-ledger backups — the missing scheduler over ledger_backup.

``ledger_backup.snapshot`` produces verified, shippable snapshots, but nothing
ever ran it: the hub's only durable state (the SQLite ledger) had no regular
backup, so losing the hub node meant losing the ledger — the single point of
failure we hit when puck dropped off the tailnet. This daemon runs on every hub
by default and, each interval, takes a verified snapshot, ships it off-box via
the sync hook, prunes old ones, and emits telemetry. It turns "a backup command
exists" into "the ledger is continuously recoverable and the hub is movable."

It is deliberately default-ON: high availability must not be opt-in. Disable
only on non-authoritative nodes via ``MAC_LEDGER_BACKUP_ENABLED=0``.

This is the RPO-bounding half of hub availability (see docs/hub-availability.md);
automated leader election/replication (rqlite/Postgres) is the RTO half.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from mac import mac_paths
from typing import Any, Dict, Mapping, Optional

from mac import ledger_backup
from mac.pg_backup import is_postgres_dsn as _is_postgres_dsn

_log = logging.getLogger("mac.ledger_backup_scheduler")

LEDGER_BACKUP_SCHEMA = "mac.ledger_backup_run.v1"

DEFAULT_INTERVAL_SECONDS = 900.0          # 15 min
MIN_INTERVAL_SECONDS = 60.0
DEFAULT_INITIAL_DELAY_SECONDS = 120.0
DEFAULT_KEEP_LAST = 14


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _default_db_path(env: Mapping[str, str]) -> str:
    raw = str(env.get("MAC_DB") or "").strip()
    if raw:
        return raw
    return str(Path(env.get("MAC_HOME") or mac_paths.mac_home()) / "mac.db")


@dataclass(frozen=True)
class LedgerBackupConfig:
    enabled: bool = True
    db_path: str = ""
    out_dir: str = ""
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    keep_last: int = DEFAULT_KEEP_LAST
    sync_cmd: str = ""

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "LedgerBackupConfig":
        env = os.environ if environ is None else environ

        def _num(name: str, default: float, low: float) -> float:
            raw = str(env.get(name) or "").strip()
            try:
                return max(low, float(raw)) if raw else default
            except ValueError:
                return default

        default_home = env.get("MAC_HOME") or str(mac_paths.mac_home())
        # A Postgres hub's authority is not the SQLite file, so snapshotting it
        # is worse than doing nothing. On rocky this ran every 15 minutes
        # against the 0-byte ~/.mac/mac.db left behind by the Postgres
        # migration: it produced a structurally valid but EMPTY snapshot, wrote
        # a correct sha256 manifest, passed its own integrity check, and rsynced
        # the result to the standby as mac-latest.db. Fourteen retained
        # "verified" backups of nothing, with every layer reporting success.
        #
        # pg_backup_scheduler owns a Postgres authority, so the two are mutually
        # exclusive on one hub and the live backup path always matches the
        # authority it claims to protect.
        authority_dsn = str(env.get("MAC_PG_BACKUP_URL") or env.get("MAC_DATABASE_URL") or "")
        return cls(
            # Default-ON, but a non-hub role should not back up.
            enabled=(
                _truthy(env.get("MAC_LEDGER_BACKUP_ENABLED", "1"))
                and str(env.get("MAC_CONTROL_PLANE_ROLE") or "hub").strip().lower() != "client"
                and not _is_postgres_dsn(authority_dsn)
            ),
            db_path=str(env.get("MAC_LEDGER_BACKUP_DB") or _default_db_path(env)),
            out_dir=str(env.get("MAC_LEDGER_BACKUP_DIR") or (Path(default_home) / "backups")),
            interval_seconds=_num("MAC_LEDGER_BACKUP_INTERVAL_SECONDS",
                                  DEFAULT_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS),
            initial_delay_seconds=_num("MAC_LEDGER_BACKUP_INITIAL_DELAY_SECONDS",
                                       DEFAULT_INITIAL_DELAY_SECONDS, 0.0),
            keep_last=int(_num("MAC_LEDGER_BACKUP_KEEP_LAST", float(DEFAULT_KEEP_LAST), 1.0)),
            sync_cmd=str(env.get(ledger_backup.SYNC_CMD_ENV) or "").strip(),
        )


class LedgerBackupScheduler:
    """Threaded daemon that takes verified ledger snapshots on an interval."""

    def __init__(
        self,
        control_plane: Any = None,
        config: Optional[LedgerBackupConfig] = None,
    ) -> None:
        self.control_plane = control_plane
        self.config = config or LedgerBackupConfig.from_env()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last: Optional[Dict[str, Any]] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        if not self.config.enabled:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="mac-ledger-backup", daemon=True)
            self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        return thread is None or not thread.is_alive()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            thread = self._thread
            last = dict(self._last) if self._last else None
        return {
            "schema": LEDGER_BACKUP_SCHEMA,
            "enabled": self.config.enabled,
            "interval_seconds": self.config.interval_seconds,
            "out_dir": self.config.out_dir,
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "last_run": last,
        }

    def _loop(self) -> None:
        if self._stop.wait(max(0.0, self.config.initial_delay_seconds)):
            return
        while not self._stop.is_set():
            self.run_once(trigger="scheduled")
            if self._stop.wait(max(MIN_INTERVAL_SECONDS, self.config.interval_seconds)):
                return

    # -- action -------------------------------------------------------------

    def run_once(self, *, trigger: str = "operator") -> Dict[str, Any]:
        """Take one verified snapshot. Never raises — a backup failure is loud
        in telemetry/notifications but must not crash the daemon or the hub."""
        result: Dict[str, Any]
        try:
            path = ledger_backup.snapshot(
                Path(self.config.db_path),
                Path(self.config.out_dir),
                keep_last=self.config.keep_last,
                sync_cmd=self.config.sync_cmd or None,
            )
            size = path.stat().st_size if path.exists() else None
            result = {"status": "ok", "trigger": trigger,
                      "snapshot": str(path), "size_bytes": size,
                      "shipped": bool(self.config.sync_cmd)}
            self._observe("ledger.backup.run", "info", result)
        except Exception as exc:  # noqa: BLE001 - backup must never kill the hub.
            result = {"status": "error", "trigger": trigger, "error": str(exc)[:500]}
            self._observe("ledger.backup.failed", "error", result)
            self._notify_failure(str(exc))
        with self._lock:
            self._last = result
        return result

    # -- telemetry ----------------------------------------------------------

    def _observe(self, event: str, level: str, detail: Dict[str, Any]) -> None:
        cp = self.control_plane
        if cp is not None and hasattr(cp, "record_log"):
            try:
                cp.record_log(event, level=level, layer="control_plane",
                              source="ledger_backup", detail=detail)
                return
            except Exception:  # noqa: BLE001 - telemetry must never raise.
                pass
        (_log.error if level == "error" else _log.info)("%s %s", event, detail)

    def _notify_failure(self, error: str) -> None:
        cp = self.control_plane
        if cp is None or not hasattr(cp, "record_notification"):
            return
        try:
            cp.record_notification(
                "ledger.backup.failed",
                "Hub ledger backup failed",
                ("A scheduled hub-ledger snapshot failed: %s\n\nThe hub is running "
                 "WITHOUT a fresh off-box backup — the ledger is at single-point-of-"
                 "failure risk until this is fixed." % error),
                subject_type="ledger_backup",
                subject_id="scheduler",
                channels=["dashboard", "hermes"],
                metadata={"error": error[:500]},
            )
        except Exception:  # noqa: BLE001 - escalation must not crash the daemon.
            _log.warning("ledger backup failure notification failed", exc_info=True)
