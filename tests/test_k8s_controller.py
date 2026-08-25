from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from mac.k8s.controller import ControllerConfig, reconcile_stuck_jobs


def _job(
    *,
    name: str,
    task_id: str = "task-1",
    lease_id: str = "lease-1",
) -> Dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": {
                "app.kubernetes.io/managed-by": "mac-k8s-runner",
                "mac.task.id": task_id,
                "mac.lease.id": lease_id,
            },
        },
        "status": {"conditions": []},
    }


class _FakeMac:
    def __init__(self, tasks: Dict[str, Dict[str, Any]]) -> None:
        self._tasks = tasks
        self.gets: List[str] = []
        self.posts: List[Dict[str, Any]] = []

    def get(self, path: str) -> Dict[str, Any]:
        self.gets.append(path)
        if path.startswith("/tasks/"):
            task_id = path[len("/tasks/") :]
            if task_id not in self._tasks:
                raise RuntimeError("404")
            return self._tasks[task_id]
        if path.startswith("/provisioning/requests"):
            return {"items": self._tasks.get("__provisioning__", [])}
        return {}

    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self.posts.append({"path": path, "body": body})
        return {}


class _FakeJobs:
    def __init__(self, listing: List[Dict[str, Any]]) -> None:
        self._listing = listing
        self.deleted: List[str] = []

    def list_active(self, namespace: str, label_selector: str) -> List[Dict[str, Any]]:
        return list(self._listing)

    def delete(self, namespace: str, name: str) -> None:
        self.deleted.append(name)

    def create(self, namespace: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
        return {}


def _cfg() -> ControllerConfig:
    return ControllerConfig(namespace="mac")


def test_live_job_is_kept() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    future = (now + timedelta(minutes=5)).isoformat()
    mac = _FakeMac(
        {
            "task-1": {
                "id": "task-1",
                "state": "running",
                "lease_id": "lease-1",
                "leased_until": future,
            }
        }
    )
    jobs = _FakeJobs([_job(name="mac-task-1")])
    summaries = reconcile_stuck_jobs(mac, jobs, _cfg(), now=now)
    assert summaries == [{"status": "kept", "reason": "live", "job": "mac-task-1"}]
    assert jobs.deleted == []


def test_terminal_task_state_deletes_job() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    mac = _FakeMac(
        {
            "task-1": {
                "id": "task-1",
                "state": "completed",
                "lease_id": "lease-1",
                "leased_until": (now + timedelta(hours=1)).isoformat(),
            }
        }
    )
    jobs = _FakeJobs([_job(name="mac-task-1")])
    summaries = reconcile_stuck_jobs(mac, jobs, _cfg(), now=now)
    assert jobs.deleted == ["mac-task-1"]
    assert summaries[0]["status"] == "deleted"
    assert summaries[0]["reason"] == "terminal-task"


def test_superseded_lease_deletes_job() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    mac = _FakeMac(
        {
            "task-1": {
                "id": "task-1",
                "state": "running",
                "lease_id": "lease-NEW",  # task was re-claimed by another runner
                "leased_until": (now + timedelta(minutes=10)).isoformat(),
            }
        }
    )
    jobs = _FakeJobs([_job(name="mac-task-1", lease_id="lease-OLD")])
    summaries = reconcile_stuck_jobs(mac, jobs, _cfg(), now=now)
    assert jobs.deleted == ["mac-task-1"]
    assert summaries[0]["reason"] == "lease-superseded"


def test_expired_lease_deletes_job() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    past = (now - timedelta(minutes=1)).isoformat()
    mac = _FakeMac(
        {
            "task-1": {
                "id": "task-1",
                "state": "running",
                "lease_id": "lease-1",
                "leased_until": past,
            }
        }
    )
    jobs = _FakeJobs([_job(name="mac-task-1")])
    summaries = reconcile_stuck_jobs(mac, jobs, _cfg(), now=now)
    assert jobs.deleted == ["mac-task-1"]
    assert summaries[0]["reason"] == "lease-expired"


def test_no_active_lease_deletes_job() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    mac = _FakeMac(
        {
            "task-1": {
                "id": "task-1",
                "state": "running",
                "lease_id": None,
                "leased_until": None,
            }
        }
    )
    jobs = _FakeJobs([_job(name="mac-task-1")])
    summaries = reconcile_stuck_jobs(mac, jobs, _cfg(), now=now)
    assert jobs.deleted == ["mac-task-1"]
    assert summaries[0]["reason"] == "no-active-lease"


def test_task_lookup_failure_keeps_job() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    mac = _FakeMac({})  # task-1 missing -> get raises
    jobs = _FakeJobs([_job(name="mac-task-1")])
    summaries = reconcile_stuck_jobs(mac, jobs, _cfg(), now=now)
    assert jobs.deleted == []
    assert summaries[0]["reason"] == "task-lookup-failed"


def test_unlabeled_job_is_kept() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    mac = _FakeMac({})
    weird = {"metadata": {"name": "rogue", "labels": {}}, "status": {}}
    summaries = reconcile_stuck_jobs(mac, _FakeJobs([weird]), _cfg(), now=now)
    assert summaries[0] == {"status": "kept", "reason": "no-task-label", "job": "rogue"}
