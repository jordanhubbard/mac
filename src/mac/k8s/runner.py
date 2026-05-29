from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from mac.k8s.config_loader import load_config_file

JsonDict = Dict[str, Any]
log = logging.getLogger(__name__)

DEFAULT_TASK_IMAGE = "ghcr.io/anthropics/mac:CHANGE-ME@sha256:CHANGE-ME"

DEFAULT_BACKOFF_LIMIT = 0  # retries owned by mac-api lease-expiry, not K8s
DEFAULT_ACTIVE_DEADLINE_SECONDS = 1800  # 30 min, override per task

DEFAULT_LEASE_RENEW_INTERVAL_SECONDS = 30

DEFAULT_JOB_POLL_INTERVAL_SECONDS = 5

DEFAULT_OPENCODE_CONFIGMAP_NAME = "mac-opencode-config"

@dataclass
class RunnerConfig:
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
    capability_filter: List[str] = field(default_factory=list)
    role_images: Dict[str, str] = field(default_factory=dict)
    role_agent_ids: Dict[str, str] = field(default_factory=dict)
    role_executors: Dict[str, str] = field(default_factory=dict)
    capability_role_aliases: Dict[str, str] = field(default_factory=dict)
    role_attestation_key_secrets: Dict[str, Dict[str, str]] = field(
        default_factory=dict
    )
    lease_renew_interval_seconds: float = float(DEFAULT_LEASE_RENEW_INTERVAL_SECONDS)
    job_poll_interval_seconds: float = float(DEFAULT_JOB_POLL_INTERVAL_SECONDS)
    opencode_configmap_name: str = DEFAULT_OPENCODE_CONFIGMAP_NAME

    @classmethod
    def from_env(cls) -> "RunnerConfig":
        unified_secret = os.environ.get("MAC_RUNNER_TASK_SECRET_NAME")

        cfg_file = load_config_file(os.environ.get("MAC_CONFIG_FILE"))

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
            role_images=cfg_file.role_images(),
            role_agent_ids=cfg_file.role_agent_ids(),
            role_executors=cfg_file.role_executors(),
            capability_role_aliases=dict(cfg_file.capability_role_aliases),
            role_attestation_key_secrets=cfg_file.role_attestation_key_secrets(),
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
            opencode_configmap_name=os.environ.get(
                "MAC_RUNNER_OPENCODE_CONFIGMAP_NAME",
                DEFAULT_OPENCODE_CONFIGMAP_NAME,
            ),
        )

def _job_name_for(task_id: str, lease_id: str) -> str:
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
    if role and role in cfg.role_agent_ids:
        return cfg.role_agent_ids[role]
    return cfg.agent_id

def _resolve_executor_for_role(
    role: Optional[str], cfg: RunnerConfig
) -> Optional[str]:
    if role and role in cfg.role_executors:
        return cfg.role_executors[role]
    return None

def _resolve_attestation_key_secret_for_role(
    role: Optional[str], cfg: RunnerConfig
) -> Optional[Dict[str, str]]:
    if not role:
        return None
    spec = cfg.role_attestation_key_secrets.get(role)
    if not spec:
        return None
    return {"name": spec["name"], "key": spec["key"]}

def _resolve_task_image(task: JsonDict, cfg: RunnerConfig) -> str:
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
    task_id = task["id"]
    lease_id = lease["id"]
    name = _job_name_for(task_id, lease_id)
    role = _resolve_task_role(task, cfg)
    job_agent_id = _resolve_agent_id_for_role(role, cfg)
    executor_cmd = _resolve_executor_for_role(role, cfg)
    attestation_secret = _resolve_attestation_key_secret_for_role(role, cfg)
    image = _resolve_task_image(task, cfg)
    active_deadline = _resolve_active_deadline(task, cfg)

    opencode_cm = (cfg.opencode_configmap_name or "").strip()
    mount_opencode = bool(opencode_cm)

    container_volume_mounts: List[JsonDict] = [
        {"name": "task-tmp", "mountPath": "/tmp"},
        {
            "name": "task-workspace",
            "mountPath": "/var/lib/mac/workspaces",
        },
    ]
    pod_volumes: List[JsonDict] = [
        {"name": "task-tmp", "emptyDir": {}},
        {
            "name": "task-workspace",
            "emptyDir": {"sizeLimit": "5Gi"},
        },
    ]
    if mount_opencode:
        container_volume_mounts.append(
            {
                "name": "opencode-config",
                "mountPath": "/etc/opencode",
                "readOnly": True,
            }
        )
        pod_volumes.append(
            {
                "name": "opencode-config",
                "configMap": {"name": opencode_cm},
            }
        )

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
    if attestation_secret is not None:
        container_env.append(
            {
                "name": "MAC_AGENT_ATTESTATION_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": attestation_secret["name"],
                        "key": attestation_secret["key"],
                    }
                },
            }
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
            "backoffLimit": cfg.backoff_limit,
            "activeDeadlineSeconds": active_deadline,
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
                            "volumeMounts": container_volume_mounts,
                        }
                    ],
                    "volumes": pod_volumes,
                },
            },
        },
    }

class MacApiProtocol(Protocol):
    def post(self, path: str, body: JsonDict) -> JsonDict: ...
    def get(self, path: str) -> JsonDict: ...

class K8sJobsProtocol(Protocol):
    def create(self, namespace: str, manifest: JsonDict) -> JsonDict: ...
    def list_active(self, namespace: str, label_selector: str) -> List[JsonDict]: ...
    def delete(self, namespace: str, name: str) -> None: ...
    def read(self, namespace: str, name: str) -> JsonDict: ...

def _job_is_terminal(job: Optional[JsonDict]) -> bool:
    if job is None:
        return False
    if job == {}:
        return True
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
    renew_interval = max(1.0, float(cfg.lease_renew_interval_seconds))
    poll_interval = max(1.0, float(cfg.job_poll_interval_seconds))
    last_renew_at = 0.0
    while not stop_event.is_set():
        # Step 1: check Job status. If terminal, stop renewing.
        try:
            job = k8s.read(namespace, job_name)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "renewal loop: read job %s/%s failed: %s",
                namespace,
                job_name,
                exc,
            )
            job = None
        if _job_is_terminal(job):
            return

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
    thread.stop_event = stop_event  # type: ignore[attr-defined]
    thread.start()
    return thread

def check_dispatcher_capabilities(
    cfg: RunnerConfig, mac: MacApiProtocol
) -> List[str]:
    if not cfg.role_agent_ids:
        return []

    try:
        dispatcher = mac.get("/agents/%s" % cfg.agent_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "dispatcher capability probe: failed to fetch dispatcher %s: %s; "
            "skipping check",
            cfg.agent_id,
            exc,
        )
        return []
    if not isinstance(dispatcher, dict):
        log.warning(
            "dispatcher capability probe: unexpected response shape for %s "
            "(got %s); skipping check",
            cfg.agent_id,
            type(dispatcher).__name__,
        )
        return []
    dispatcher_caps = {
        str(c) for c in (dispatcher.get("capabilities") or [])
    }

    union_role_caps: set = set()
    for role, role_agent_id in cfg.role_agent_ids.items():
        try:
            role_agent = mac.get("/agents/%s" % role_agent_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "dispatcher capability probe: failed to fetch role agent "
                "role=%s agent=%s: %s; skipping role in coverage check",
                role,
                role_agent_id,
                exc,
            )
            continue
        if not isinstance(role_agent, dict):
            log.warning(
                "dispatcher capability probe: unexpected response shape for "
                "role agent %s (got %s); skipping role in coverage check",
                role_agent_id,
                type(role_agent).__name__,
            )
            continue
        for cap in role_agent.get("capabilities") or []:
            union_role_caps.add(str(cap))

    missing = sorted(union_role_caps - dispatcher_caps)
    return missing

def claim_and_launch_one(
    mac: MacApiProtocol,
    k8s: K8sJobsProtocol,
    cfg: RunnerConfig,
) -> Optional[JsonDict]:
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

    role = _resolve_task_role(task, cfg)
    job_agent_id = _resolve_agent_id_for_role(role, cfg)
    if job_agent_id != cfg.agent_id:
        try:
            mac.post(
                "/leases/%s/delegate" % lease["id"],
                {"agent_id": cfg.agent_id, "to_agent_id": job_agent_id},
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "lease delegation failed for task=%s lease=%s to=%s: %s",
                task.get("id"),
                lease.get("id"),
                job_agent_id,
                exc,
            )
            try:
                mac.post(
                    "/tasks/%s/transition" % task["id"],
                    {
                        "target_state": "open",
                        "actor": cfg.agent_id,
                        "detail": {
                            "reason": "lease_delegation_failed",
                            "lease_id": lease["id"],
                            "to_agent_id": job_agent_id,
                            "error": str(exc),
                        },
                    },
                )
            except Exception:  # noqa: BLE001
                pass
            return {
                "status": "lease_delegation_failed",
                "task_id": task.get("id"),
                "lease_id": lease.get("id"),
                "to_agent_id": job_agent_id,
                "error": str(exc),
            }

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

def runner_loop(
    mac: MacApiProtocol,
    k8s: K8sJobsProtocol,
    cfg: RunnerConfig,
    *,
    iterations: Optional[int] = None,
    sleep: Optional[Any] = None,
) -> int:
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
