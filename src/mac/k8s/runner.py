"""``mac-k8s-runner`` — turns ready tasks into Kubernetes Jobs.

Architecture (K8s Phase 4):

  mac-api (durable coordinator)
       ▲
       │ HTTP: GET /agents/{id}/claim-next, POST /leases/{id}/renew
       │
  mac-k8s-runner (Deployment, N replicas)
       │
       │ K8s API: batch/v1 Job create
       ▼
  task-Job pods (one per claimed lease)
       │
       │ mac-task-runner exec → mac-api: POST /tasks/{id}/evidence,
       │                                 POST /tasks/{id}/submit-for-review
       ▼
  exit 0 (Job marks Complete)

The runner does **not** execute tasks itself. It only claims-and-spawns,
keeping permissions tight: the runner ServiceAccount needs `batch.jobs`
CRUD in its namespace; individual task-Job pods need only an
``MAC_WORKER_TOKEN`` mounted via Secret. The runner is multi-replica;
the existing ``uniq_leases_active_per_task`` partial unique index in
Postgres prevents duplicate-launch races.

This module is split for testability:

* ``build_job_spec(task, lease, cfg)`` returns a pure-dict K8s Job
  manifest — no I/O, easily unit-testable.
* ``claim_and_launch_one(mac_client, k8s_client, cfg)`` is the
  imperative step (claim a task, create the Job, return a summary).
* ``runner_loop(...)`` is the long-running entry point used by the
  binary; covered by an integration test rather than unit tests.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

JsonDict = Dict[str, Any]
log = logging.getLogger(__name__)

# Default image used when a task's runtime_environment manifest does not
# pin one. Operators override via MAC_RUNNER_DEFAULT_IMAGE.
DEFAULT_TASK_IMAGE = "ghcr.io/anthropics/mac:CHANGE-ME@sha256:CHANGE-ME"

DEFAULT_BACKOFF_LIMIT = 0  # retries owned by mac-api lease-expiry, not K8s
DEFAULT_ACTIVE_DEADLINE_SECONDS = 1800  # 30 min, override per task


@dataclass
class RunnerConfig:
    """Static configuration for a ``mac-k8s-runner`` replica."""

    mac_url: str
    agent_id: str
    namespace: str = "mac"
    service_account: str = "mac-task-runner"
    default_image: str = DEFAULT_TASK_IMAGE
    secret_name_for_token: str = "mac-api-config"
    secret_key_for_token: str = "MAC_WORKER_TOKEN"
    secret_name_for_secret_key: str = "mac-api-config"
    secret_key_for_secret_key: str = "MAC_SECRET_KEY"
    poll_interval_seconds: float = 5.0
    backoff_limit: int = DEFAULT_BACKOFF_LIMIT
    active_deadline_seconds: int = DEFAULT_ACTIVE_DEADLINE_SECONDS
    # Subset of capabilities this runner is willing to dispatch. Tasks
    # whose required_capabilities are not a subset are ignored. Empty =
    # accept anything the agent record's capabilities can satisfy.
    capability_filter: List[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "RunnerConfig":
        # MAC_RUNNER_TASK_SECRET_NAME overrides BOTH defaults at once for
        # the common case where MAC_WORKER_TOKEN + MAC_SECRET_KEY live in
        # the same Kubernetes Secret (e.g. operator-supplied
        # `mac-api-config` or an ExternalSecret target like `mac-secret`).
        # Operators that need different Secrets per key can override each
        # one individually via the per-key vars below.
        unified_secret = os.environ.get("MAC_RUNNER_TASK_SECRET_NAME")
        return cls(
            mac_url=os.environ.get("MAC_URL") or os.environ.get("MAC_HUB_URL", ""),
            agent_id=os.environ.get("MAC_AGENT_ID", "mac-k8s-runner"),
            namespace=os.environ.get("MAC_RUNNER_NAMESPACE", "mac"),
            service_account=os.environ.get(
                "MAC_RUNNER_TASK_SERVICE_ACCOUNT", "mac-task-runner"
            ),
            default_image=os.environ.get(
                "MAC_RUNNER_DEFAULT_IMAGE", DEFAULT_TASK_IMAGE
            ),
            secret_name_for_token=os.environ.get(
                "MAC_RUNNER_TASK_TOKEN_SECRET_NAME", unified_secret or "mac-api-config"
            ),
            secret_key_for_token=os.environ.get(
                "MAC_RUNNER_TASK_TOKEN_SECRET_KEY", "MAC_WORKER_TOKEN"
            ),
            secret_name_for_secret_key=os.environ.get(
                "MAC_RUNNER_TASK_SECRET_KEY_SECRET_NAME", unified_secret or "mac-api-config"
            ),
            secret_key_for_secret_key=os.environ.get(
                "MAC_RUNNER_TASK_SECRET_KEY_SECRET_KEY", "MAC_SECRET_KEY"
            ),
            poll_interval_seconds=float(
                os.environ.get("MAC_RUNNER_POLL_INTERVAL_SECONDS", "5")
            ),
            active_deadline_seconds=int(
                os.environ.get(
                    "MAC_RUNNER_ACTIVE_DEADLINE_SECONDS",
                    str(DEFAULT_ACTIVE_DEADLINE_SECONDS),
                )
            ),
            capability_filter=[
                c.strip()
                for c in os.environ.get("MAC_RUNNER_CAPABILITIES", "").split(",")
                if c.strip()
            ],
        )


# ----------------------------------------------------------------------
# Pure logic: build the K8s Job manifest from a claimed task/lease.
# ----------------------------------------------------------------------

def _job_name_for(task_id: str, lease_id: str) -> str:
    # K8s name length cap is 63 chars; use a short hash of the lease id
    # to keep names unique across re-runs without exposing the full id.
    short_lease = lease_id.split("-")[-1][:12]
    raw = "mac-task-%s-%s" % (task_id, short_lease)
    return _sanitize_dns_label(raw)


def _sanitize_dns_label(value: str) -> str:
    out = []
    for ch in value.lower():
        if ch.isalnum() or ch == "-":
            out.append(ch)
        else:
            out.append("-")
    label = "".join(out).strip("-")[:63] or "mac-task"
    if not label[0].isalnum():
        label = "x" + label[1:]
    return label


def _resolve_task_image(task: JsonDict, cfg: RunnerConfig) -> str:
    """Pick the container image for this task's Job.

    Resolution order:
      1. task.metadata.runtime.image  (per-task override)
      2. task.metadata.k8s.image      (operator escape hatch)
      3. cfg.default_image
    """
    meta = task.get("metadata") or {}
    runtime = meta.get("runtime") or {}
    if isinstance(runtime, dict) and runtime.get("image"):
        return str(runtime["image"])
    k8s = meta.get("k8s") or {}
    if isinstance(k8s, dict) and k8s.get("image"):
        return str(k8s["image"])
    return cfg.default_image


def _resolve_active_deadline(task: JsonDict, cfg: RunnerConfig) -> int:
    meta = task.get("metadata") or {}
    k8s = meta.get("k8s") or {}
    if isinstance(k8s, dict) and k8s.get("active_deadline_seconds"):
        try:
            return max(60, int(k8s["active_deadline_seconds"]))
        except (TypeError, ValueError):
            pass
    return cfg.active_deadline_seconds


def build_job_spec(
    task: JsonDict,
    lease: JsonDict,
    cfg: RunnerConfig,
) -> JsonDict:
    """Return a ``batch/v1`` Job manifest for one claimed task/lease.

    The returned dict is exactly what you would ``kubectl apply``. The
    runner passes it to the K8s API directly via the kubernetes client.
    """
    task_id = task["id"]
    lease_id = lease["id"]
    name = _job_name_for(task_id, lease_id)
    image = _resolve_task_image(task, cfg)
    active_deadline = _resolve_active_deadline(task, cfg)

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": cfg.namespace,
            "labels": {
                "app.kubernetes.io/name": "mac-task",
                "app.kubernetes.io/component": "task-executor",
                "app.kubernetes.io/managed-by": "mac-k8s-runner",
                "mac.task.id": _sanitize_dns_label(task_id),
                "mac.lease.id": _sanitize_dns_label(lease_id),
                "mac.runner.agent": _sanitize_dns_label(cfg.agent_id),
            },
            "annotations": {
                "mac.task/title": str(task.get("title") or "")[:240],
                "mac.task/required-capabilities": ",".join(
                    str(c) for c in (task.get("required_capabilities") or [])
                ),
            },
        },
        "spec": {
            # backoffLimit=0: retries are owned by mac-api lease-expiry
            # + task.max_attempts, NOT by the K8s Job controller. A
            # crash leaves the lease to expire; the next runner cycle
            # observes the open task and launches a fresh Job.
            "backoffLimit": cfg.backoff_limit,
            "activeDeadlineSeconds": active_deadline,
            # Auto-clean Jobs ~24h after completion to avoid clutter; the
            # durable execution record lives in Postgres.
            "ttlSecondsAfterFinished": 24 * 3600,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "mac-task",
                        "app.kubernetes.io/component": "task-executor",
                        "mac.task.id": _sanitize_dns_label(task_id),
                        "mac.lease.id": _sanitize_dns_label(lease_id),
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": cfg.service_account,
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "runAsNonRoot": True,
                        "fsGroup": 1000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "mac-task-runner",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["mac-task-runner"],
                            "env": [
                                {"name": "MAC_URL", "value": cfg.mac_url},
                                {"name": "MAC_TASK_ID", "value": task_id},
                                {"name": "MAC_LEASE_ID", "value": lease_id},
                                {
                                    "name": "MAC_AGENT_ID",
                                    "value": cfg.agent_id,
                                },
                                {
                                    "name": "MAC_WORKER_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": cfg.secret_name_for_token,
                                            "key": cfg.secret_key_for_token,
                                        }
                                    },
                                },
                                {
                                    "name": "MAC_SECRET_KEY",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": cfg.secret_name_for_secret_key,
                                            "key": cfg.secret_key_for_secret_key,
                                        }
                                    },
                                },
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "2", "memory": "2Gi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": [
                                {"name": "task-tmp", "mountPath": "/tmp"},
                                {
                                    "name": "task-workspace",
                                    "mountPath": "/var/lib/mac/workspaces",
                                },
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "task-tmp", "emptyDir": {}},
                        {
                            "name": "task-workspace",
                            "emptyDir": {"sizeLimit": "5Gi"},
                        },
                    ],
                },
            },
        },
    }


# ----------------------------------------------------------------------
# Imperative step: claim one task, launch one Job.
# ----------------------------------------------------------------------

class MacApiProtocol(Protocol):
    """Minimal protocol the runner needs from a mac-api HTTP client.

    Implemented for production by ``mac.hermes_adapter.MacApiClient``;
    for tests, a tiny fake.
    """

    def post(self, path: str, body: JsonDict) -> JsonDict: ...
    def get(self, path: str) -> JsonDict: ...


class K8sJobsProtocol(Protocol):
    """Minimal protocol the runner needs from a K8s Jobs client.

    Backed in production by ``kubernetes.client.BatchV1Api`` (its
    ``create_namespaced_job`` accepts a dict). For tests, a fake.
    """

    def create(self, namespace: str, manifest: JsonDict) -> JsonDict: ...
    def list_active(self, namespace: str, label_selector: str) -> List[JsonDict]: ...
    def delete(self, namespace: str, name: str) -> None: ...


def claim_and_launch_one(
    mac: MacApiProtocol,
    k8s: K8sJobsProtocol,
    cfg: RunnerConfig,
) -> Optional[JsonDict]:
    """Claim one ready task and create the corresponding K8s Job.

    Returns a summary dict describing what was done, or ``None`` when
    no task was ready / eligible. Idempotent at the partial-unique-index
    level: two runners racing for the same task will see exactly one
    successful claim because of ``uniq_leases_active_per_task``.
    """
    # Use the existing claim-next endpoint so dispatcher policy
    # (tenant fairness, capability filter, role binding) is respected.
    payload: JsonDict = {}
    if cfg.capability_filter:
        payload["capabilities"] = cfg.capability_filter
    try:
        assignment = mac.post(
            "/agents/%s/claim-next" % cfg.agent_id, payload
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("claim-next failed: %s", exc)
        return None
    if not assignment or not assignment.get("task"):
        return None

    task = assignment["task"]
    lease = assignment.get("lease") or {}
    if not lease.get("id"):
        log.error(
            "claim-next returned a task without a lease; refusing to launch (task=%s)",
            task.get("id"),
        )
        return None

    manifest = build_job_spec(task, lease, cfg)
    try:
        created = k8s.create(cfg.namespace, manifest)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "k8s Job create failed for task=%s lease=%s: %s",
            task.get("id"),
            lease["id"],
            exc,
        )
        # Best-effort: release the lease so another runner can retry.
        try:
            mac.post(
                "/tasks/%s/transition" % task["id"],
                {
                    "target_state": "open",
                    "actor": cfg.agent_id,
                    "detail": {
                        "reason": "k8s_job_create_failed",
                        "error": str(exc),
                    },
                },
            )
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": "k8s_create_failed",
            "task_id": task.get("id"),
            "lease_id": lease.get("id"),
            "error": str(exc),
        }

    return {
        "status": "launched",
        "task_id": task["id"],
        "lease_id": lease["id"],
        "job_name": manifest["metadata"]["name"],
        "job_uid": (created.get("metadata") or {}).get("uid"),
        "image": manifest["spec"]["template"]["spec"]["containers"][0]["image"],
    }


# ----------------------------------------------------------------------
# Long-running loop. Covered by an integration test; unit tests target
# the pure functions above.
# ----------------------------------------------------------------------

def runner_loop(
    mac: MacApiProtocol,
    k8s: K8sJobsProtocol,
    cfg: RunnerConfig,
    *,
    iterations: Optional[int] = None,
    sleep: Optional[Any] = None,
) -> int:
    """Run the claim+launch cycle.

    `iterations` caps the loop (used by tests). `sleep` is a sleep-like
    callable, default ``time.sleep``. Returns the count of Jobs
    successfully launched.
    """
    sleeper = sleep or time.sleep
    launched = 0
    i = 0
    while iterations is None or i < iterations:
        i += 1
        result = claim_and_launch_one(mac, k8s, cfg)
        if result and result.get("status") == "launched":
            launched += 1
            log.info(
                "launched job task=%s lease=%s job=%s",
                result["task_id"],
                result["lease_id"],
                result["job_name"],
            )
        else:
            # No work or failure: back off a poll interval.
            sleeper(cfg.poll_interval_seconds)
    return launched
