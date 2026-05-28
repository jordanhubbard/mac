"""``mac-k8s-controller`` — Phase 5 reconciliation loop.

Two reconcilers, both safe to run from a single Deployment:

* ``reconcile_stuck_jobs(mac, k8s, cfg)`` — lists active K8s Jobs
  labelled ``app.kubernetes.io/managed-by=mac-k8s-runner`` and deletes
  any whose corresponding mac-api task either (a) no longer has the
  same active lease the Job claims, or (b) is already in a terminal
  state. This is the cleanup path for evicted/OOMKilled Jobs whose
  parent task already got re-claimed by another runner.

* ``reconcile_provisioning_requests(mac, k8s, cfg, scaler)`` — lists
  open ``agent_provisioning_requests`` and asks the injected
  ``scaler`` to bump the relevant worker-pool Deployment's replica
  count. The scaler is injectable because the right policy
  (per-role pool, shared pool, custom CRD) is cluster-specific.

CRDs are intentionally NOT introduced in Phase 5 MVP. The goal allows
"optionally introduce CRDs" — we ship without them so the controller
runs against any cluster, then a follow-up can promote
``agent_provisioning_requests`` and rollouts to first-class CRDs once
the API surface stabilises.
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
    # Reconciliation cadence (seconds) — small enough that an
    # OOMKilled Job is cleaned up within one lease TTL.
    reconcile_interval_seconds: float = 30.0


# ----------------------------------------------------------------------
# Stuck-Job reconciliation
# ----------------------------------------------------------------------

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
    """Delete K8s Jobs whose task is no longer claimed under the same lease.

    A Job is considered "stuck" when ANY of:
      - The task no longer has an active lease at all.
      - The task's active lease id differs from the Job's mac.lease.id label.
      - The task's state is terminal (completed / failed / cancelled).
      - The task's lease has expired (leased_until < now).

    Returns the list of action summaries — one entry per Job inspected,
    each describing whether it was deleted and why.
    """
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


# ----------------------------------------------------------------------
# Provisioning request reconciliation
# ----------------------------------------------------------------------

class WorkerPoolScaler(Protocol):
    """Strategy for converting an open provisioning request into a
    scale action. Implementations are cluster-specific (one-pool-per-role
    Deployments, HPA + queue depth metric, custom CRD reconciler, etc.)
    so the controller injects the scaler rather than hard-wiring one.
    """

    def scale_for(
        self, request: JsonDict
    ) -> Optional[JsonDict]: ...  # returns summary or None


def reconcile_provisioning_requests(
    mac: MacApiProtocol,
    cfg: ControllerConfig,
    scaler: WorkerPoolScaler,
) -> List[JsonDict]:
    """Process open ``agent_provisioning_requests`` via the injected scaler.

    Returns one summary per request encountered. The controller does NOT
    close requests — the scaler (or a downstream provisioner) is
    responsible for moving them from ``open`` to ``fulfilled`` once
    capacity is up. This keeps the controller idempotent: repeated runs
    re-emit the same scale action, the scaler decides whether it's a
    no-op.
    """
    summaries: List[JsonDict] = []
    try:
        page = mac.get("/provisioning/requests?status=open")
    except Exception as exc:  # noqa: BLE001
        log.error("list provisioning-requests failed: %s", exc)
        return [{"status": "list-failed", "error": str(exc)}]

    requests = page.get("items") or page.get("requests") or []
    for req in requests:
        try:
            action = scaler.scale_for(req)
        except Exception as exc:  # noqa: BLE001
            summaries.append(
                {
                    "status": "scaler-error",
                    "request_id": req.get("id"),
                    "error": str(exc),
                }
            )
            continue
        if action is None:
            summaries.append(
                {
                    "status": "skipped",
                    "request_id": req.get("id"),
                    "reason": "scaler returned None",
                }
            )
        else:
            summaries.append(
                {
                    "status": "scaled",
                    "request_id": req.get("id"),
                    "action": action,
                }
            )
    return summaries


# ----------------------------------------------------------------------
# A reference scaler: one Deployment per role, scale up by 1 per request.
#
# Not used by default; ships as a starting point operators can adapt.
# ----------------------------------------------------------------------

class K8sDeploymentScaler:
    """Bumps a Deployment's spec.replicas by `delta` per provisioning request.

    Naming convention: each role-slug maps to a Deployment
    ``mac-worker-<role-slug>`` in the same namespace. Operators using a
    different naming scheme override `deployment_name_for` in a
    subclass.
    """

    def __init__(
        self,
        k8s_apps: "K8sDeploymentsProtocol",
        namespace: str,
        max_replicas_per_role: int = 10,
        delta_per_request: int = 1,
    ) -> None:
        self._k8s_apps = k8s_apps
        self._namespace = namespace
        self._max = max_replicas_per_role
        self._delta = delta_per_request

    def deployment_name_for(self, role_slug: str) -> str:
        return "mac-worker-" + role_slug

    def scale_for(self, request: JsonDict) -> Optional[JsonDict]:
        role_slug = (request.get("role_slug") or "").strip()
        if not role_slug:
            return None
        name = self.deployment_name_for(role_slug)
        current = self._k8s_apps.get_deployment(self._namespace, name)
        if current is None:
            return None
        cur_replicas = int(((current.get("spec") or {}).get("replicas")) or 0)
        target = min(self._max, cur_replicas + self._delta)
        if target <= cur_replicas:
            return {"deployment": name, "no_op": True, "replicas": cur_replicas}
        self._k8s_apps.scale_deployment(self._namespace, name, target)
        return {
            "deployment": name,
            "from": cur_replicas,
            "to": target,
            "role_slug": role_slug,
        }


class K8sDeploymentsProtocol(Protocol):
    def get_deployment(self, namespace: str, name: str) -> Optional[JsonDict]: ...
    def scale_deployment(self, namespace: str, name: str, replicas: int) -> None: ...
