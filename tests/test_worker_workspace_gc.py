from __future__ import annotations

import os
import threading
import time
import types

import mac.worker_workspace_gc as wgc
from mac.worker_workspace_gc import WorkspaceGCMixin


class _Harness(WorkspaceGCMixin):
    def __init__(self, workspace, active=""):
        self.workspace = workspace
        self.agent_id = "agent_test"
        self._active = active
        self.logs = []
        self._workspace_gc_lock = threading.Lock()
        self._workspace_gc_thread = None
        self._last_workspace_gc_at = 0.0

    def _observe_log(self, name, level="info", detail=None, **kw):
        self.logs.append((name, level, detail or {}))

    def _active_task_id(self):
        return self._active


def _mk(workspace, name, age_hours=0.0):
    d = workspace / name
    d.mkdir()
    (d / "f").write_text("x", encoding="utf-8")
    t = time.time() - age_hours * 3600.0
    os.utime(d, (t, t))
    return d


def _fake_disk(monkeypatch, free_gb):
    monkeypatch.setattr(
        wgc.shutil,
        "disk_usage",
        lambda p: types.SimpleNamespace(total=0, used=0, free=int(free_gb * wgc._GB)),
    )


def test_prunes_aged_beyond_keep_window_disk_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_KEEP_RECENT", "2")
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_MAX_AGE_HOURS", "1")
    _fake_disk(monkeypatch, 500.0)  # plenty of disk -> not low
    # 5 dirs, all older than max_age, staggered newest->oldest
    dirs = [_mk(tmp_path, "task_%d" % i, age_hours=2 + i) for i in range(5)]
    h = _Harness(tmp_path, active="task_current")  # known active -> exact keep
    res = h._gc_workspaces_once()
    # the 2 newest survive (keep window); the 3 oldest (aged, outside window) go
    assert res["removed"] == 3
    assert dirs[0].exists() and dirs[1].exists()
    assert not dirs[2].exists() and not dirs[3].exists() and not dirs[4].exists()


def test_recent_kept_when_not_aged_and_disk_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_KEEP_RECENT", "1")
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_MAX_AGE_HOURS", "24")
    _fake_disk(monkeypatch, 500.0)
    dirs = [_mk(tmp_path, "task_%d" % i, age_hours=0.1 * i) for i in range(4)]
    h = _Harness(tmp_path)
    res = h._gc_workspaces_once()
    # none are aged and disk is fine -> nothing removed
    assert res["removed"] == 0
    assert all(d.exists() for d in dirs)


def test_active_task_workspace_is_protected(tmp_path, monkeypatch):
    from mac.worker import _safe_path_component

    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_KEEP_RECENT", "0")
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_MAX_AGE_HOURS", "1")
    _fake_disk(monkeypatch, 500.0)
    active_id = "task_deadbeef"
    active_dir = _mk(tmp_path, _safe_path_component(active_id), age_hours=99)
    other = _mk(tmp_path, "task_old", age_hours=99)
    h = _Harness(tmp_path, active=active_id)
    res = h._gc_workspaces_once()
    # active dir excluded from candidates entirely; the aged 'other' is removed
    assert active_dir.exists()
    assert not other.exists()
    assert res["removed"] == 1


def test_low_disk_prunes_even_non_aged(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_KEEP_RECENT", "1")
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_MAX_AGE_HOURS", "24")
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_MIN_FREE_GB", "10")
    _fake_disk(monkeypatch, 2.0)  # 2GB free < 10GB min -> low disk
    dirs = [_mk(tmp_path, "task_%d" % i, age_hours=0.01 * i) for i in range(5)]
    h = _Harness(tmp_path, active="task_current")  # known active -> exact keep
    res = h._gc_workspaces_once()
    # low disk: keep only the newest (keep=1), prune the other 4 despite fresh
    assert res["removed"] == 4
    assert res["low_disk"] is True
    assert dirs[0].exists()


def test_disk_low_escalation_is_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_MIN_FREE_GB", "10")
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_HIGH_WATER_GB", "10")
    _fake_disk(monkeypatch, 3.0)  # stays low before and after
    _mk(tmp_path, "task_old", age_hours=99)
    h = _Harness(tmp_path)
    h._gc_workspaces_once()
    names = [name for name, _lvl, _d in h.logs]
    assert "worker.workspace_gc.disk_low" in names
    warn = next(d for n, lvl, d in h.logs if n == "worker.workspace_gc.disk_low")
    assert warn["free_gb_after"] < warn["high_water_gb"]


def test_non_dir_entries_and_missing_root(tmp_path, monkeypatch):
    _fake_disk(monkeypatch, 500.0)
    (tmp_path / ".mac-agentbus-control.json").write_text("{}", encoding="utf-8")
    _mk(tmp_path, "task_old", age_hours=99)
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_MAX_AGE_HOURS", "1")
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_KEEP_RECENT", "0")
    h = _Harness(tmp_path, active="task_current")  # known active -> exact keep
    res = h._gc_workspaces_once()
    assert res["removed"] == 1  # only the dir, control file untouched
    assert (tmp_path / ".mac-agentbus-control.json").exists()
    # missing root fails safe
    missing = _Harness(tmp_path / "nope")
    assert missing._gc_workspaces_once()["status"] == "no_root"


def test_maybe_start_is_interval_gated_and_off_by_flag(tmp_path, monkeypatch):
    _fake_disk(monkeypatch, 500.0)
    h = _Harness(tmp_path)
    # disabled -> no thread
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_ENABLED", "0")
    h._maybe_start_workspace_gc()
    assert h._workspace_gc_thread is None
    # enabled -> starts once, then interval-gated
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_ENABLED", "1")
    monkeypatch.setenv("MAC_WORKER_WORKSPACE_GC_INTERVAL_SECONDS", "9999")
    h._maybe_start_workspace_gc()
    first = h._workspace_gc_thread
    assert first is not None
    first.join(timeout=5)
    h._maybe_start_workspace_gc()  # within interval -> not restarted
    assert h._workspace_gc_thread is first
