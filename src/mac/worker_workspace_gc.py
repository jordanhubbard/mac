"""Worker workspace garbage collection (task_02ebb6c4).

Every node runs a worker, and each task gets its own worktree under
``self.workspace`` (agent-workspaces). Nothing pruned the workspaces of
COMPLETED tasks, so they accumulated without bound and filled the disk. On the
GKE pods (49G) this reached 100%, which silently broke the coding-route probe
(it cannot write its proof) -> ``coding_agent_route_unverified`` -> the worker
was excluded from ALL repo-coupled coding tasks and went idle. The larger-disk
LAN nodes were accumulating the same way (80-110G each), just further from the
wall — so this is a fleet-wide worker self-health gap, not a GKE quirk.

Audit context: a prior standalone ``scripts/cleanup_artifacts.py`` was added but
NEVER wired to any scheduler and was later deleted (its automation follow-up was
tracked in the since-removed beads ledger, so it was lost). The only live
cleanup — ``worker_repo_prep``'s per-task reclamation — reclaims just the CURRENT
task's own worktree, never the backlog. This mixin is the single consolidated
fix: a periodic, free-space-watermark-driven bulk GC that runs on EVERY worker,
protects the active task and the N most-recent workspaces, prunes the rest
(aggressively when the disk is low), and escalates loudly when the disk stays
low after a sweep.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

JsonDict = Dict[str, Any]
_GB = 1024**3


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else int(default)
    except (TypeError, ValueError):
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


class WorkspaceGCMixin:
    """Periodic, free-space-aware GC of completed-task workspaces.

    Relies on MacWorker providing: ``self.workspace`` (Path), ``self.agent_id``,
    ``self._observe_log``, ``self._active_task_id()``. State
    (``_workspace_gc_lock`` / ``_workspace_gc_thread`` / ``_last_workspace_gc_at``)
    is initialised in MacWorker.__init__.
    """

    def _maybe_start_workspace_gc(self) -> None:
        """Interval-gated: spawn a background thread to GC stale workspaces.

        Runs on every worker (``MAC_WORKER_WORKSPACE_GC_ENABLED`` default on).
        The sweep runs OFF the poll thread so a large ``rmtree`` cannot stall
        heartbeats or claiming (mirrors the coding-route probe / delivery drain).
        """
        if not _env_bool("MAC_WORKER_WORKSPACE_GC_ENABLED", True):
            return
        with self._workspace_gc_lock:
            thread = self._workspace_gc_thread
            if thread is not None and thread.is_alive():
                return
            interval = max(30.0, _env_float("MAC_WORKER_WORKSPACE_GC_INTERVAL_SECONDS", 600.0))
            now = time.monotonic()
            if self._last_workspace_gc_at and now - self._last_workspace_gc_at < interval:
                return
            self._last_workspace_gc_at = now
            t = threading.Thread(
                target=self._gc_workspaces_safe,
                name="mac-workspace-gc-%s" % self.agent_id,
                daemon=True,
            )
            self._workspace_gc_thread = t
            t.start()

    def _gc_workspaces_safe(self) -> None:
        try:
            self._gc_workspaces_once()
        except Exception as exc:  # noqa: BLE001 - GC must never crash the worker.
            self._observe_log(
                "worker.workspace_gc.error",
                level="warning",
                detail={"agent_id": self.agent_id, "error": str(exc)},
            )

    def _active_workspace_name(self) -> Tuple[str, bool]:
        """Return (protected_dir_name, active_unknown).

        The active task's worktree is named ``_safe_path_component(task_id)``.
        Imported lazily to avoid a circular import at module load. On any
        failure we report ``active_unknown=True`` so the caller keeps an extra
        most-recent dir as a fail-safe.
        """
        try:
            from mac.worker import _safe_path_component

            active = self._active_task_id()
            if active:
                return _safe_path_component(active), False
        except Exception:  # noqa: BLE001 - protection is best-effort; fail safe.
            return "", True
        return "", True

    def _gc_workspaces_once(self) -> JsonDict:
        root = Path(self.workspace)
        if not root.is_dir():
            return {"status": "no_root"}

        min_free_gb = _env_float("MAC_WORKER_WORKSPACE_GC_MIN_FREE_GB", 10.0)
        high_water_gb = _env_float("MAC_WORKER_WORKSPACE_GC_HIGH_WATER_GB", min_free_gb)
        keep_recent = max(0, _env_int("MAC_WORKER_WORKSPACE_GC_KEEP_RECENT", 20))
        max_age_hours = _env_float("MAC_WORKER_WORKSPACE_GC_MAX_AGE_HOURS", 24.0)

        free_before_gb = shutil.disk_usage(str(root)).free / _GB
        low_disk = free_before_gb < min_free_gb

        protected_name, active_unknown = self._active_workspace_name()

        entries: List[Tuple[Path, float]] = []
        for child in root.iterdir():
            if not child.is_dir():
                continue  # skip control-state files and other artefacts
            if protected_name and child.name == protected_name:
                continue  # never remove the active task's worktree
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            entries.append((child, mtime))
        entries.sort(key=lambda e: e[1], reverse=True)  # newest first

        # When we could not resolve the active task, keep one extra most-recent
        # dir (the likely-active one) as a fail-safe.
        keep = keep_recent + (1 if active_unknown else 0)
        now = time.time()
        max_age_s = max_age_hours * 3600.0
        removed = 0
        for idx, (child, mtime) in enumerate(entries):
            # The most-recent window (newest ``keep`` dirs) is ALWAYS protected;
            # the active task's dir is already excluded above. Outside the
            # window, remove when the disk is low (aggressive reclaim) OR the
            # workspace is older than max_age (routine hygiene).
            if idx < keep:
                continue
            aged = (now - mtime) > max_age_s
            if low_disk or aged:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1

        free_after_gb = shutil.disk_usage(str(root)).free / _GB
        result: JsonDict = {
            "status": "ok",
            "removed": removed,
            "freed_gb": round(max(0.0, free_after_gb - free_before_gb), 2),
            "free_gb_before": round(free_before_gb, 2),
            "free_gb_after": round(free_after_gb, 2),
            "low_disk": low_disk,
            "kept": min(keep, len(entries)),
        }
        if removed:
            self._observe_log(
                "worker.workspace_gc.completed",
                level="info",
                detail={"agent_id": self.agent_id, **result},
            )
        # Loud escalation: if the disk is STILL below the high-water mark after
        # a sweep, surface it (the silent-starvation guard) instead of quietly
        # going route-unverified.
        if free_after_gb < high_water_gb:
            self._observe_log(
                "worker.workspace_gc.disk_low",
                level="warning",
                detail={
                    "agent_id": self.agent_id,
                    "high_water_gb": high_water_gb,
                    **result,
                },
            )
        return result
