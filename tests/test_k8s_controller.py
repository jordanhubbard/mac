"""Phase 5 controller tests — stuck-Job reconciliation + provisioning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from mac.k8s.controller import (
    ControllerConfig,
    K8sDeploymentScaler,
    reconcile_provisioning_requests,
    reconcile_stuck_jobs,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

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
            task_id = path[len("/tasks/"):]
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


# ----------------------------------------------------------------------
# reconcile_stuck_jobs
# ----------------------------------------------------------------------

def test_live_job_is_kept() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    future = (now + timedelta(minutes=5)).isoformat()
    mac = _FakeMac({"task-1": {
        "id": "task-1",
        "state": "running",
        "lease_id": "lease-1",
        "leased_until": future,
    }})
    jobs = _FakeJobs([_job(name="mac-task-1")])
    summaries = reconcile_stuck_jobs(mac, jobs, _cfg(), now=now)
    assert summaries == [{"status": "kept", "reason": "live", "job": "mac-task-1"}]
    assert jobs.deleted == []


def test_terminal_task_state_deletes_job() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    mac = _FakeMac({"task-1": {
        "id": "task-1",
        "state": "completed",
        "lease_id": "lease-1",
        "leased_until": (now + timedelta(hours=1)).isoformat(),
    }})
    jobs = _FakeJobs([_job(name="mac-task-1")])
    summaries = reconcile_stuck_jobs(mac, jobs, _cfg(), now=now)
    assert jobs.deleted == ["mac-task-1"]
    assert summaries[0]["status"] == "deleted"
    assert summaries[0]["reason"] == "terminal-task"


def test_superseded_lease_deletes_job() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    mac = _FakeMac({"task-1": {
        "id": "task-1",
        "state": "running",
        "lease_id": "lease-NEW",  # task was re-claimed by another runner
        "leased_until": (now + timedelta(minutes=10)).isoformat(),
    }})
    jobs = _FakeJobs([_job(name="mac-task-1", lease_id="lease-OLD")])
    summaries = reconcile_stuck_jobs(mac, jobs, _cfg(), now=now)
    assert jobs.deleted == ["mac-task-1"]
    assert summaries[0]["reason"] == "lease-superseded"


def test_expired_lease_deletes_job() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    past = (now - timedelta(minutes=1)).isoformat()
    mac = _FakeMac({"task-1": {
        "id": "task-1",
        "state": "running",
        "lease_id": "lease-1",
        "leased_until": past,
    }})
    jobs = _FakeJobs([_job(name="mac-task-1")])
    summaries = reconcile_stuck_jobs(mac, jobs, _cfg(), now=now)
    assert jobs.deleted == ["mac-task-1"]
    assert summaries[0]["reason"] == "lease-expired"


def test_no_active_lease_deletes_job() -> None:
    now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    mac = _FakeMac({"task-1": {
        "id": "task-1",
        "state": "running",
        "lease_id": None,
        "leased_until": None,
    }})
    jobs = _FakeJobs([_job(name="mac-task-1")])
    summaries = reconcile_stuck_jobs(mac, jobs, _cfg(), now=now)
    assert jobs.deleted == ["mac-task-1"]
    assert summaries[0]["reason"] == "no-active-lease"


def test_task_lookup_failure_keeps_job() -> None:
    # If we can't tell whether the task is still alive, don't kill the Job.
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


# ----------------------------------------------------------------------
# reconcile_provisioning_requests + scaler
# ----------------------------------------------------------------------

class _FakeScaler:
    def __init__(self, returning: Optional[Dict[str, Any]] = None, raises: bool = False) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._returning = returning
        self._raises = raises

    def scale_for(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self.calls.append(request)
        if self._raises:
            raise RuntimeError("boom")
        return self._returning


def test_provisioning_no_requests_returns_empty() -> None:
    mac = _FakeMac({"__provisioning__": []})
    scaler = _FakeScaler()
    out = reconcile_provisioning_requests(mac, _cfg(), scaler)
    assert out == []
    assert scaler.calls == []


def test_provisioning_dispatches_to_scaler() -> None:
    mac = _FakeMac({"__provisioning__": [
        {"id": "req-1", "role_slug": "python-worker", "status": "open"},
        {"id": "req-2", "role_slug": "ops-worker", "status": "open"},
    ]})
    scaler = _FakeScaler(returning={"deployment": "x", "from": 1, "to": 2})
    out = reconcile_provisioning_requests(mac, _cfg(), scaler)
    assert [s["request_id"] for s in out] == ["req-1", "req-2"]
    assert all(s["status"] == "scaled" for s in out)
    assert len(scaler.calls) == 2


def test_provisioning_scaler_error_is_isolated() -> None:
    mac = _FakeMac({"__provisioning__": [
        {"id": "req-1", "role_slug": "python-worker"},
    ]})
    scaler = _FakeScaler(raises=True)
    out = reconcile_provisioning_requests(mac, _cfg(), scaler)
    assert out[0]["status"] == "scaler-error"
    assert "boom" in out[0]["error"]


def test_provisioning_skipped_when_scaler_returns_none() -> None:
    mac = _FakeMac({"__provisioning__": [{"id": "req-1", "role_slug": ""}]})
    scaler = _FakeScaler(returning=None)
    out = reconcile_provisioning_requests(mac, _cfg(), scaler)
    assert out[0]["status"] == "skipped"


# ----------------------------------------------------------------------
# Reference K8sDeploymentScaler
# ----------------------------------------------------------------------

class _FakeDeployments:
    def __init__(self, deployments: Dict[str, Dict[str, Any]]) -> None:
        self._d = deployments
        self.scales: List[Dict[str, Any]] = []

    def get_deployment(self, namespace: str, name: str) -> Optional[Dict[str, Any]]:
        return self._d.get(name)

    def scale_deployment(self, namespace: str, name: str, replicas: int) -> None:
        self.scales.append({"name": name, "replicas": replicas})


def test_deployment_scaler_bumps_replicas() -> None:
    deploys = _FakeDeployments({
        "mac-worker-python": {"spec": {"replicas": 1}},
    })
    scaler = K8sDeploymentScaler(deploys, namespace="mac", max_replicas_per_role=5)
    action = scaler.scale_for({"role_slug": "python", "status": "open"})
    assert action == {
        "deployment": "mac-worker-python",
        "from": 1,
        "to": 2,
        "role_slug": "python",
    }
    assert deploys.scales == [{"name": "mac-worker-python", "replicas": 2}]


def test_deployment_scaler_respects_max() -> None:
    deploys = _FakeDeployments({
        "mac-worker-x": {"spec": {"replicas": 5}},
    })
    scaler = K8sDeploymentScaler(deploys, namespace="mac", max_replicas_per_role=5)
    action = scaler.scale_for({"role_slug": "x", "status": "open"})
    assert action == {"deployment": "mac-worker-x", "no_op": True, "replicas": 5}
    assert deploys.scales == []


def test_deployment_scaler_skips_unknown_deployment() -> None:
    deploys = _FakeDeployments({})
    scaler = K8sDeploymentScaler(deploys, namespace="mac")
    assert scaler.scale_for({"role_slug": "unknown", "status": "open"}) is None


def test_deployment_scaler_skips_request_without_role() -> None:
    deploys = _FakeDeployments({"mac-worker-x": {"spec": {"replicas": 1}}})
    scaler = K8sDeploymentScaler(deploys, namespace="mac")
    assert scaler.scale_for({"role_slug": "", "status": "open"}) is None
