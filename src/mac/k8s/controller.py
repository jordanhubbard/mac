"""Kubernetes controller configuration and reconciliation types.

Defines the controller configuration dataclass and the label selectors and
helpers used to reconcile MAC-managed Kubernetes resources.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

JsonDict = Dict[str, Any]
log = logging.getLogger(__name__)

MANAGED_LABEL_SELECTOR = "app.kubernetes.io/managed-by=mac-k8s-runner"

@dataclass
class ControllerConfig:
    namespace: str = "mac"
    reconcile_interval_seconds: float = 30.0

class MacApiProtocol(Protocol):
    def get(self, path: str) -> JsonDict: ...
    def post(self, path: str, body: JsonDict) -> JsonDict: ...

class K8sJobsProtocol(Protocol):
    def list_active(self, namespace: str, label_selector: str) -> List[JsonDict]: ...
    def delete(self, namespace: str, name: str) -> None: ...

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Tolerate trailing Z without isoformat support pre-3.11.
        v = value.replace("Z", "+00:00")
        return datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None

def _job_lease_label(job: JsonDict) -> Optional[str]:
    labels = ((job.get("metadata") or {}).get("labels") or {})
    return labels.get("mac.lease.id")

def _job_task_label(job: JsonDict) -> Optional[str]:
    labels = ((job.get("metadata") or {}).get("labels") or {})
    return labels.get("mac.task.id")

def _job_name(job: JsonDict) -> Optional[str]:
    return (job.get("metadata") or {}).get("name")

def reconcile_stuck_jobs(
    mac: MacApiProtocol,
    k8s: K8sJobsProtocol,
    cfg: ControllerConfig,
    *,
    now: Optional[datetime] = None,
) -> List[JsonDict]:
    """Reconcile active managed jobs and reap those that are stuck."""
    now = now or datetime.now(timezone.utc)
    summaries: List[JsonDict] = []
    try:
        jobs = k8s.list_active(cfg.namespace, MANAGED_LABEL_SELECTOR)
    except Exception as exc:  # noqa: BLE001
        log.error("list_active jobs failed: %s", exc)
        return [{"status": "list-failed", "error": str(exc)}]

    for job in jobs:
        name = _job_name(job) or "?"
        task_id = _job_task_label(job)
        lease_id_on_job = _job_lease_label(job)
        if not task_id:
            summaries.append(
                {"status": "kept", "reason": "no-task-label", "job": name}
            )
            continue
        try:
            task = mac.get("/tasks/%s" % task_id)
        except Exception as exc:  # noqa: BLE001
            summaries.append(
                {
                    "status": "kept",
                    "reason": "task-lookup-failed",
                    "job": name,
                    "task_id": task_id,
                    "error": str(exc),
                }
            )
            continue

        state = (task.get("state") or "").lower()
        if state in ("completed", "failed", "cancelled"):
            _delete_job(k8s, cfg.namespace, name, summaries, reason="terminal-task")
            continue

        active_lease_id = task.get("lease_id")
        if active_lease_id and lease_id_on_job and active_lease_id != lease_id_on_job:
            _delete_job(
                k8s, cfg.namespace, name, summaries,
                reason="lease-superseded",
                extra={"job_lease": lease_id_on_job, "task_lease": active_lease_id},
            )
            continue

        if not active_lease_id:
            _delete_job(
                k8s, cfg.namespace, name, summaries,
                reason="no-active-lease",
            )
            continue

        leased_until = _parse_iso(task.get("leased_until"))
        if leased_until and leased_until < now:
            _delete_job(
                k8s, cfg.namespace, name, summaries,
                reason="lease-expired",
                extra={"leased_until": task.get("leased_until")},
            )
            continue

        summaries.append({"status": "kept", "reason": "live", "job": name})

    return summaries

def _delete_job(
    k8s: K8sJobsProtocol,
    namespace: str,
    name: str,
    summaries: List[JsonDict],
    *,
    reason: str,
    extra: Optional[JsonDict] = None,
) -> None:
    record = {"status": "deleted", "reason": reason, "job": name}
    if extra:
        record.update(extra)
    try:
        k8s.delete(namespace, name)
        summaries.append(record)
    except Exception as exc:  # noqa: BLE001
        record["status"] = "delete-failed"
        record["error"] = str(exc)
        summaries.append(record)

