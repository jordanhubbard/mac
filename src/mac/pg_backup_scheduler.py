"""Scheduled, observable PostgreSQL authority backups — the Postgres analogue of
``mac.ledger_backup_scheduler``.

``pg_backup.dump`` produces a consistent, owner-only, restore-verified artifact,
but something has to run it on a cadence and page an operator when it fails.
This daemon does for the Postgres tier exactly what the ledger scheduler does
for SQLite: each interval it takes a verified dump, ships it off-box via the
sync hook, prunes old ones, runs a periodic restore-to-scratch drill, and emits
telemetry. A dump/verify/ship failure is loud in the ledger and as an operator
notification, but never crashes the daemon or the hub.

It is default-ON *only* when the hub authority is PostgreSQL (``MAC_DATABASE_URL``
is a ``postgres://`` DSN) and the role is not ``client``. On a SQLite hub it is
a no-op — the SQLite ledger scheduler owns that tier — and there is deliberately
NO SQLite fallback path here: a PostgreSQL failure is surfaced, not silently
downgraded to a SQLite backup.

The restore drill (proving schema + representative row counts come back from the
artifact) runs every ``MAC_PG_BACKUP_VERIFY_EVERY`` runs (default: every run) so
operators can trade drill cost for cadence without ever losing the artifact.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from mac import mac_paths, pg_backup

_log = logging.getLogger("mac.pg_backup_scheduler")

PG_BACKUP_SCHEMA = "mac.pg_backup_run.v1"

DEFAULT_INTERVAL_SECONDS = 3600.0          # 60 min
MIN_INTERVAL_SECONDS = 60.0
DEFAULT_INITIAL_DELAY_SECONDS = 120.0
DEFAULT_KEEP_LAST = 14
DEFAULT_VERIFY_EVERY = 1


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


# Re-exported from pg_backup so ledger_backup_scheduler and this module cannot
# drift on what counts as a Postgres authority.
_is_postgres_dsn = pg_backup.is_postgres_dsn


@dataclass(frozen=True)
class PgBackupConfig:
    enabled: bool = False
    dsn: str = ""
    out_dir: str = ""
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    keep_last: int = DEFAULT_KEEP_LAST
    verify_every: int = DEFAULT_VERIFY_EVERY
    sync_cmd: str = ""

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "PgBackupConfig":
        env = os.environ if environ is None else environ

        def _num(name: str, default: float, low: float) -> float:
            raw = str(env.get(name) or "").strip()
            try:
                return max(low, float(raw)) if raw else default
            except ValueError:
                return default

        dsn = str(env.get("MAC_PG_BACKUP_URL") or env.get("MAC_DATABASE_URL") or "").strip()
        role = str(env.get("MAC_CONTROL_PLANE_ROLE") or "hub").strip().lower()
        default_home = env.get("MAC_HOME") or str(mac_paths.mac_home())
        enabled = (
            _truthy(env.get("MAC_PG_BACKUP_ENABLED", "1"))
            and role != "client"
            and _is_postgres_dsn(dsn)
        )
        return cls(
            enabled=enabled,
            dsn=dsn,
            out_dir=str(
                env.get("MAC_PG_BACKUP_DIR")
                or env.get("MAC_LEDGER_BACKUP_DIR")
                or (Path(default_home) / "backups")
            ),
            interval_seconds=_num("MAC_PG_BACKUP_INTERVAL_SECONDS",
                                  DEFAULT_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS),
            initial_delay_seconds=_num("MAC_PG_BACKUP_INITIAL_DELAY_SECONDS",
                                       DEFAULT_INITIAL_DELAY_SECONDS, 0.0),
            keep_last=int(_num("MAC_PG_BACKUP_KEEP_LAST", float(DEFAULT_KEEP_LAST), 1.0)),
            verify_every=int(_num("MAC_PG_BACKUP_VERIFY_EVERY",
                                  float(DEFAULT_VERIFY_EVERY), 1.0)),
            sync_cmd=str(env.get(pg_backup.SYNC_CMD_ENV) or "").strip(),
        )


class PgBackupScheduler:
    """Threaded daemon that takes verified PostgreSQL backups on an interval."""

    def __init__(
        self,
        control_plane: Any = None,
        config: Optional[PgBackupConfig] = None,
    ) -> None:
        self.control_plane = control_plane
        self.config = config or PgBackupConfig.from_env()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last: Optional[Dict[str, Any]] = None
        self._run_count = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        if not self.config.enabled:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="mac-pg-backup", daemon=True)
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
            "schema": PG_BACKUP_SCHEMA,
            "enabled": self.config.enabled,
            "interval_seconds": self.config.interval_seconds,
            "out_dir": self.config.out_dir,
            "verify_every": self.config.verify_every,
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
        """Take one verified backup. Never raises — a failure is loud in
        telemetry/notifications but must not crash the daemon or the hub. A
        PostgreSQL backup failure is surfaced, never silently downgraded to
        SQLite."""
        with self._lock:
            self._run_count += 1
            run_index = self._run_count
        every = max(1, self.config.verify_every)
        do_verify = (run_index % every) == 0
        result: Dict[str, Any]
        try:
            outcome = pg_backup.dump(
                self.config.dsn,
                Path(self.config.out_dir),
                keep_last=self.config.keep_last,
                verify=do_verify,
                sync_cmd=self.config.sync_cmd or None,
            )
            result = {
                "status": "ok",
                "trigger": trigger,
                "artifact": str(outcome.path),
                "sha256": outcome.sha256,
                "size_bytes": outcome.size_bytes,
                "restore_verified": outcome.verified,
                "verify_performed": do_verify,
                "shipped": bool(self.config.sync_cmd),
            }
            self._observe("pg.backup.run", "info", result)
        except Exception as exc:  # noqa: BLE001 - backup must never kill the hub.
            result = {"status": "error", "trigger": trigger,
                      "verify_performed": do_verify, "error": str(exc)[:500]}
            self._observe("pg.backup.failed", "error", result)
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
                              source="pg_backup", detail=detail)
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
                "pg.backup.failed",
                "Hub PostgreSQL backup failed",
                ("A scheduled PostgreSQL authority backup failed: %s\n\nThe hub is "
                 "running WITHOUT a fresh, restore-verified off-box backup. There is "
                 "NO SQLite fallback — the PostgreSQL authority is at recovery risk "
                 "until this is fixed." % error),
                subject_type="pg_backup",
                subject_id="scheduler",
                channels=["dashboard", "hermes"],
                metadata={"error": error[:500]},
            )
        except Exception:  # noqa: BLE001 - escalation must not crash the daemon.
            _log.warning("pg backup failure notification failed", exc_info=True)
