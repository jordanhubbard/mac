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

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

JsonDict = Dict[str, Any]
log = logging.getLogger(__name__)

# Default image used when a task's runtime_environment manifest does not
# pin one. Operators override via MAC_RUNNER_DEFAULT_IMAGE.
DEFAULT_TASK_IMAGE = "ghcr.io/anthropics/mac:CHANGE-ME@sha256:CHANGE-ME"

DEFAULT_BACKOFF_LIMIT = 0  # retries owned by mac-api lease-expiry, not K8s
DEFAULT_ACTIVE_DEADLINE_SECONDS = 1800  # 30 min, override per task

# How frequently the runner-side renewal goroutine renews each Job's
# lease. Matches the cadence previously used inside the Job pod (see
# job_executor.py's deprecated LEASE_RENEW_INTERVAL_SECONDS). Operators
# can override via MAC_RUNNER_LEASE_RENEW_INTERVAL_SECONDS.
DEFAULT_LEASE_RENEW_INTERVAL_SECONDS = 30

# How frequently the renewal goroutine polls the Job's status to learn
# whether it's terminal. Override via MAC_RUNNER_JOB_POLL_INTERVAL_SECONDS.
DEFAULT_JOB_POLL_INTERVAL_SECONDS = 5


def _json_env(name: str, default: Dict[str, str]) -> Dict[str, str]:
    """Read a JSON-encoded ``Dict[str, str]`` from env.

    Returns ``default`` (a fresh copy) when the var is unset OR when the
    value is malformed; in the malformed case we log a warning rather
    than crashing so a typo in operator config never bricks the runner.
    """
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return dict(default)
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        log.warning("malformed JSON in env %s; falling back to default: %s", name, exc)
        return dict(default)
    if not isinstance(loaded, dict):
        log.warning(
            "env %s did not decode to a JSON object (got %s); using default",
            name,
            type(loaded).__name__,
        )
        return dict(default)
    return {str(k): str(v) for k, v in loaded.items()}


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
    # Role specialisation maps. All default to {} so an unset operator
    # config is bit-for-bit identical to today: no role lookups, no
    # alias resolution, MAC_AGENT_ROLE/MAC_TASK_EXECUTOR_COMMAND absent
    # or empty in the Job env.
    role_images: Dict[str, str] = field(default_factory=dict)
    role_agent_ids: Dict[str, str] = field(default_factory=dict)
    role_executors: Dict[str, str] = field(default_factory=dict)
    capability_role_aliases: Dict[str, str] = field(default_factory=dict)
    # How often (seconds) the runner renews a Job's lease. Stays in
    # config so tests can shorten it. Defaults match the cadence
    # previously used by the in-Job renewal thread.
    lease_renew_interval_seconds: float = float(DEFAULT_LEASE_RENEW_INTERVAL_SECONDS)
    # How often (seconds) the runner polls Job status to detect terminal
    # state and stop renewing.
    job_poll_interval_seconds: float = float(DEFAULT_JOB_POLL_INTERVAL_SECONDS)

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
            role_images=_json_env("MAC_RUNNER_ROLE_IMAGES", {}),
            role_agent_ids=_json_env("MAC_RUNNER_ROLE_AGENT_IDS", {}),
            role_executors=_json_env("MAC_RUNNER_ROLE_EXECUTORS", {}),
            capability_role_aliases=_json_env(
                "MAC_RUNNER_CAPABILITY_ROLE_ALIASES", {}
            ),
            lease_renew_interval_seconds=float(
                os.environ.get(
                    "MAC_RUNNER_LEASE_RENEW_INTERVAL_SECONDS",
                    str(DEFAULT_LEASE_RENEW_INTERVAL_SECONDS),
                )
            ),
            job_poll_interval_seconds=float(
                os.environ.get(
                    "MAC_RUNNER_JOB_POLL_INTERVAL_SECONDS",
                    str(DEFAULT_JOB_POLL_INTERVAL_SECONDS),
                )
            ),
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


def _resolve_task_role(task: JsonDict, cfg: RunnerConfig) -> Optional[str]:
    """Pick the role for this task. See spec §6.1.

    Resolution order:
      1. ``task.metadata.required_role`` (explicit operator override)
      2. First capability in ``task.required_capabilities`` that
         appears in ``cfg.capability_role_aliases`` (declared order).
      3. ``None`` — falls through to default agent + default image.

    Crucially there is **no** naked first-capability fallback: an
    unaliased capability cannot silently mint a role. This is the
    codex review M1 finding.
    """
    meta = task.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("required_role"):
        return str(meta["required_role"])
    aliases = cfg.capability_role_aliases or {}
    if aliases:
        for cap in task.get("required_capabilities") or []:
            role = aliases.get(str(cap))
            if role:
                return str(role)
    return None


def _resolve_agent_id_for_role(role: Optional[str], cfg: RunnerConfig) -> str:
    """Map a role to the agent_id used in the Job pod env.

    Falls back to ``cfg.agent_id`` (the dispatcher identity) when the
    role is unset or unmapped — so unspecialised tasks are bit-for-bit
    identical to today.
    """
    if role and role in cfg.role_agent_ids:
        return cfg.role_agent_ids[role]
    return cfg.agent_id


def _resolve_executor_for_role(
    role: Optional[str], cfg: RunnerConfig
) -> Optional[str]:
    """Map a role to its executor command, or ``None`` to let the
    Job executor fall back to its existing default (i.e. read
    ``MAC_TASK_EXECUTOR_COMMAND`` from env as it does today)."""
    if role and role in cfg.role_executors:
        return cfg.role_executors[role]
    return None


def _resolve_task_image(task: JsonDict, cfg: RunnerConfig) -> str:
    """Pick the container image for this task's Job.

    Resolution order:
      1. ``task.metadata.runtime.image`` (per-task override)
      2. ``task.metadata.k8s.image`` (operator escape hatch)
      3. Role-mapped image via ``cfg.role_images`` (role resolved by
         ``_resolve_task_role``).
      4. ``cfg.default_image``.
    """
    meta = task.get("metadata") or {}
    runtime = meta.get("runtime") or {}
    if isinstance(runtime, dict) and runtime.get("image"):
        return str(runtime["image"])
    k8s = meta.get("k8s") or {}
    if isinstance(k8s, dict) and k8s.get("image"):
        return str(k8s["image"])
    role = _resolve_task_role(task, cfg)
    if role and role in cfg.role_images:
        return cfg.role_images[role]
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
    # Resolve role-derived values once. For unspecialised tasks (no
    # role hit) these collapse to today's values: job_agent_id ==
    # cfg.agent_id, executor_cmd is None (so MAC_TASK_EXECUTOR_COMMAND
    # is not emitted; the Job executor honours whatever the operator
    # set elsewhere), MAC_AGENT_ROLE is "".
    role = _resolve_task_role(task, cfg)
    job_agent_id = _resolve_agent_id_for_role(role, cfg)
    executor_cmd = _resolve_executor_for_role(role, cfg)
    image = _resolve_task_image(task, cfg)
    active_deadline = _resolve_active_deadline(task, cfg)

    container_env: List[JsonDict] = [
        {"name": "MAC_URL", "value": cfg.mac_url},
        {"name": "MAC_TASK_ID", "value": task_id},
        {"name": "MAC_LEASE_ID", "value": lease_id},
        {"name": "MAC_AGENT_ID", "value": job_agent_id},
        {"name": "MAC_AGENT_ROLE", "value": role or ""},
    ]
    if executor_cmd is not None:
        container_env.append(
            {"name": "MAC_TASK_EXECUTOR_COMMAND", "value": executor_cmd}
        )
    container_env.extend(
        [
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
        ]
    )

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
                "mac.role": _sanitize_dns_label(role or "default"),
                "mac.agent.id": _sanitize_dns_label(job_agent_id),
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
                            "env": container_env,
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

    ``read`` is used by the runner-side lease renewal loop (PR1) to
    detect when a Job hits a terminal status so renewal can stop.
    Implementations should return a dict shaped like the K8s Job
    object — at minimum ``{"status": {"succeeded": int, "failed": int}}``.
    """

    def create(self, namespace: str, manifest: JsonDict) -> JsonDict: ...
    def list_active(self, namespace: str, label_selector: str) -> List[JsonDict]: ...
    def delete(self, namespace: str, name: str) -> None: ...
    def read(self, namespace: str, name: str) -> JsonDict: ...


def _job_is_terminal(job: JsonDict) -> bool:
    """Return True iff a Job's status reports succeeded or failed pods.

    Mirrors the kube-client's V1JobStatus shape: ``status.succeeded`` and
    ``status.failed`` are integer counts of pods in those terminal
    states. Either being ``>= 1`` is sufficient (with ``backoffLimit=0``
    one failed pod means the Job won't be retried).
    """
    status = (job.get("status") or {}) if isinstance(job, dict) else {}
    try:
        succeeded = int(status.get("succeeded") or 0)
    except (TypeError, ValueError):
        succeeded = 0
    try:
        failed = int(status.get("failed") or 0)
    except (TypeError, ValueError):
        failed = 0
    return succeeded >= 1 or failed >= 1


def _lease_renewal_loop(
    mac: MacApiProtocol,
    k8s: K8sJobsProtocol,
    cfg: RunnerConfig,
    *,
    namespace: str,
    job_name: str,
    lease_id: str,
    agent_id: str,
    stop_event: threading.Event,
    sleeper: Callable[[float], None],
) -> None:
    """Renew ``lease_id`` until the Job hits a terminal status.

    Owned by the runner Deployment per spec §6.3: the Job pod no longer
    renews; the runner is the single source of authority over the
    lease's deadline. ``agent_id`` here is the **dispatcher** identity
    (``cfg.agent_id``) — the same identity that performed the claim, so
    mac-api sees a consistent authorisation chain.

    The loop:
      * Polls Job status every ``cfg.job_poll_interval_seconds``. On
        terminal status, exits without renewing further.
      * Renews the lease every ``cfg.lease_renew_interval_seconds``.
      * Tolerates transient renew + status-read failures: logs and
        continues. Only ``stop_event`` or terminal Job status terminates
        the loop.
    """
    renew_interval = max(1.0, float(cfg.lease_renew_interval_seconds))
    poll_interval = max(1.0, float(cfg.job_poll_interval_seconds))
    last_renew_at = 0.0
    while not stop_event.is_set():
        # Step 1: check Job status. If terminal, stop renewing.
        try:
            job = k8s.read(namespace, job_name)
        except Exception as exc:  # noqa: BLE001
            # Don't kill the goroutine on a transient read error. The
            # next tick may succeed; in the worst case the lease still
            # expires server-side and the controller cleans up.
            log.warning(
                "renewal loop: read job %s/%s failed: %s",
                namespace,
                job_name,
                exc,
            )
            job = {}
        if job and _job_is_terminal(job):
            return

        # Step 2: maybe renew the lease. We piggy-back on the same
        # tick as the status poll for simplicity, gated on the renew
        # interval so we don't over-renew when poll_interval is short.
        now = time.monotonic()
        if now - last_renew_at >= renew_interval:
            try:
                mac.post(
                    "/leases/%s/renew" % lease_id,
                    {"agent_id": agent_id},
                )
                last_renew_at = now
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "renewal loop: POST /leases/%s/renew failed: %s",
                    lease_id,
                    exc,
                )
                # Leave last_renew_at as-is so the next tick retries.

        # Step 3: sleep until the next poll tick, breaking early if
        # stop_event fires.
        if stop_event.wait(poll_interval):
            return


def _start_lease_renewal_thread(
    mac: MacApiProtocol,
    k8s: K8sJobsProtocol,
    cfg: RunnerConfig,
    *,
    namespace: str,
    job_name: str,
    lease_id: str,
    agent_id: str,
    sleeper: Optional[Callable[[float], None]] = None,
) -> threading.Thread:
    """Spawn a daemon thread running ``_lease_renewal_loop``.

    The thread is daemon=True so a runner shutdown doesn't deadlock on
    in-flight renewal threads. If the runner crashes mid-Job the
    renewal stops, the lease expires, and the next runner replica (or
    controller) reclaims — same outcome as the in-Job renewer
    crashing in the previous design.
    """
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_lease_renewal_loop,
        kwargs={
            "mac": mac,
            "k8s": k8s,
            "cfg": cfg,
            "namespace": namespace,
            "job_name": job_name,
            "lease_id": lease_id,
            "agent_id": agent_id,
            "stop_event": stop_event,
            "sleeper": sleeper or time.sleep,
        },
        name="mac-runner-lease-%s" % lease_id[:12],
        daemon=True,
    )
    # Attach the stop event so callers (tests, future graceful
    # shutdown) can cancel.
    thread.stop_event = stop_event  # type: ignore[attr-defined]
    thread.start()
    return thread


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

    # PR2c (spec §6.3, Option B): when the resolved role agent differs
    # from the dispatcher, delegate the lease so the role agent's Job
    # pod can author start_task / submit_for_review / add_evidence
    # against the hub. The owner (this runner) still owns renewal +
    # release. Failure is non-fatal — we log and continue. The Job's
    # own start_task call will surface the error if delegation didn't
    # take, and the runner-owned lease will expire normally.
    role = _resolve_task_role(task, cfg)
    job_agent_id = _resolve_agent_id_for_role(role, cfg)
    if job_agent_id != cfg.agent_id:
        try:
            mac.post(
                "/leases/%s/delegate" % lease["id"],
                {"agent_id": cfg.agent_id, "to_agent_id": job_agent_id},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "lease delegation failed for task=%s lease=%s to=%s: %s",
                task.get("id"),
                lease.get("id"),
                job_agent_id,
                exc,
            )

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

    # Per spec §6.3, the runner Deployment owns lease renewal. Spawn a
    # daemon thread that keeps the lease alive until the Job hits a
    # terminal status. The Job pod itself no longer renews.
    job_name = manifest["metadata"]["name"]
    try:
        _start_lease_renewal_thread(
            mac,
            k8s,
            cfg,
            namespace=cfg.namespace,
            job_name=job_name,
            lease_id=lease["id"],
            agent_id=cfg.agent_id,
        )
    except Exception as exc:  # noqa: BLE001
        # The Job already exists; failing to start the renewal thread
        # is non-fatal here. The lease will expire and the controller
        # will re-claim — same recovery path as a runner crash.
        log.error(
            "failed to start renewal thread for task=%s lease=%s: %s",
            task["id"],
            lease["id"],
            exc,
        )

    return {
        "status": "launched",
        "task_id": task["id"],
        "lease_id": lease["id"],
        "job_name": job_name,
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
