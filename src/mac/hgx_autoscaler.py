"""Background reconciliation from durable MAC demand to bounded HGX capacity.

The dispatcher never blocks on provider work. Provisioning request
notifications only wake this service; every decision is rebuilt from the
durable request ledger. Scale-up requires sustained demand and advances in
small steps. Scale-down is deliberately slower and may delete only old,
controller-created sessions that never became registered MAC agents.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from mac.env_config import env_bool, env_float, env_int, env_str
from mac.hgx_elastic_capacity import (
    DEFAULT_STATE_PATH,
    HgxCapacityPolicy,
    HgxElasticCapacityController,
)
from mac.hgx_provider import HgxError, HgxProvider
from mac.models import (
    AgentProvisioningRequest,
    NotFoundError,
    TERMINAL_TASK_STATES,
    TaskState,
)


AUTOSCALER_SCHEMA = "mac.hgx_autoscaler.v1"
HGX_SCALABLE_REQUEST_REASONS = frozenset({"dispatch.no_eligible_agent"})
TASK_BOUND_REQUEST_REASONS = frozenset(
    {
        "dispatch.no_eligible_agent",
        "review.no_eligible_reviewer",
    }
)
DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_INITIAL_DELAY_SECONDS = 15.0
DEFAULT_SCALE_UP_STABILIZATION_SECONDS = 120.0
DEFAULT_SCALE_DOWN_STABILIZATION_SECONDS = 3600.0
DEFAULT_SPARE_MIN_AGE_SECONDS = 3600.0

_log = logging.getLogger("mac.hgx_autoscaler")


@dataclass(frozen=True)
class HgxAutoscalerConfig:
    enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    scale_up_stabilization_seconds: float = (
        DEFAULT_SCALE_UP_STABILIZATION_SECONDS
    )
    scale_down_stabilization_seconds: float = (
        DEFAULT_SCALE_DOWN_STABILIZATION_SECONDS
    )
    spare_min_age_seconds: float = DEFAULT_SPARE_MIN_AGE_SECONDS
    scale_up_step: int = 1
    scale_down_step: int = 1
    min_ready: int = 0
    max_sessions: int = 10
    headroom: int = 0
    cluster: str = "gke-newhouse"
    gpu_count: int = 1
    memory_gib: int = 64
    cpu_count: int = 8
    cooldown_seconds: float = 300.0
    wait_timeout_seconds: float = 300.0
    poll_interval_seconds: float = 5.0
    hgx_binary: str = "hgx"
    hgx_command_timeout_seconds: float = 120.0
    state_path: str = DEFAULT_STATE_PATH
    registered_agents_file: str = ""
    name_prefix: str = "mac-fungible"
    configuration_error: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and not self.configuration_error

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "active": self.active}

    def capacity_policy(self) -> HgxCapacityPolicy:
        return HgxCapacityPolicy(
            min_ready=self.min_ready,
            max_sessions=self.max_sessions,
            headroom=self.headroom,
            cluster=self.cluster,
            gpu_count=self.gpu_count,
            memory_gib=self.memory_gib,
            cpu_count=self.cpu_count,
            max_create_per_run=self.scale_up_step,
            cooldown_seconds=self.cooldown_seconds,
            wait_timeout_seconds=self.wait_timeout_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
        )

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "HgxAutoscalerConfig":
        env = environ
        config = cls(
            enabled=env_bool("MAC_HGX_AUTOSCALE_ENABLED", False, environ=env),
            interval_seconds=env_float(
                "MAC_HGX_AUTOSCALE_INTERVAL_SECONDS",
                DEFAULT_INTERVAL_SECONDS,
                minimum=15.0,
                maximum=3600.0,
                environ=env,
            ),
            initial_delay_seconds=env_float(
                "MAC_HGX_AUTOSCALE_INITIAL_DELAY_SECONDS",
                DEFAULT_INITIAL_DELAY_SECONDS,
                minimum=0.0,
                maximum=3600.0,
                environ=env,
            ),
            scale_up_stabilization_seconds=env_float(
                "MAC_HGX_AUTOSCALE_SCALE_UP_STABILIZATION_SECONDS",
                DEFAULT_SCALE_UP_STABILIZATION_SECONDS,
                minimum=0.0,
                maximum=24 * 3600.0,
                environ=env,
            ),
            scale_down_stabilization_seconds=env_float(
                "MAC_HGX_AUTOSCALE_SCALE_DOWN_STABILIZATION_SECONDS",
                DEFAULT_SCALE_DOWN_STABILIZATION_SECONDS,
                minimum=60.0,
                maximum=30 * 24 * 3600.0,
                environ=env,
            ),
            spare_min_age_seconds=env_float(
                "MAC_HGX_AUTOSCALE_SPARE_MIN_AGE_SECONDS",
                DEFAULT_SPARE_MIN_AGE_SECONDS,
                minimum=60.0,
                maximum=30 * 24 * 3600.0,
                environ=env,
            ),
            scale_up_step=env_int(
                "MAC_HGX_AUTOSCALE_SCALE_UP_STEP",
                1,
                minimum=1,
                maximum=10,
                environ=env,
            ),
            scale_down_step=env_int(
                "MAC_HGX_AUTOSCALE_SCALE_DOWN_STEP",
                1,
                minimum=1,
                maximum=10,
                environ=env,
            ),
            min_ready=env_int(
                "MAC_HGX_AUTOSCALE_MIN_READY",
                0,
                minimum=0,
                maximum=100,
                environ=env,
            ),
            max_sessions=env_int(
                "MAC_HGX_AUTOSCALE_MAX_SESSIONS",
                10,
                minimum=1,
                maximum=100,
                environ=env,
            ),
            headroom=env_int(
                "MAC_HGX_AUTOSCALE_HEADROOM",
                0,
                minimum=0,
                maximum=100,
                environ=env,
            ),
            cluster=env_str(
                "MAC_HGX_AUTOSCALE_CLUSTER", "gke-newhouse", environ=env
            ),
            gpu_count=env_int(
                "MAC_HGX_AUTOSCALE_GPU",
                1,
                minimum=0,
                maximum=8,
                environ=env,
            ),
            memory_gib=env_int(
                "MAC_HGX_AUTOSCALE_MEMORY_GIB",
                64,
                minimum=8,
                maximum=256,
                environ=env,
            ),
            cpu_count=env_int(
                "MAC_HGX_AUTOSCALE_CPU",
                8,
                minimum=1,
                maximum=64,
                environ=env,
            ),
            cooldown_seconds=env_float(
                "MAC_HGX_AUTOSCALE_COOLDOWN_SECONDS",
                300.0,
                minimum=0.0,
                maximum=24 * 3600.0,
                environ=env,
            ),
            wait_timeout_seconds=env_float(
                "MAC_HGX_AUTOSCALE_WAIT_TIMEOUT_SECONDS",
                300.0,
                minimum=1.0,
                maximum=3600.0,
                environ=env,
            ),
            poll_interval_seconds=env_float(
                "MAC_HGX_AUTOSCALE_POLL_INTERVAL_SECONDS",
                5.0,
                minimum=0.1,
                maximum=60.0,
                environ=env,
            ),
            hgx_binary=env_str("MAC_HGX_BINARY", "hgx", environ=env),
            hgx_command_timeout_seconds=env_float(
                "MAC_HGX_COMMAND_TIMEOUT_SECONDS",
                120.0,
                minimum=1.0,
                maximum=3600.0,
                environ=env,
            ),
            state_path=env_str(
                "MAC_HGX_AUTOSCALE_STATE_FILE", DEFAULT_STATE_PATH, environ=env
            ),
            registered_agents_file=env_str(
                "MAC_HGX_REGISTERED_AGENTS_FILE", "", environ=env
            ),
            name_prefix=env_str(
                "MAC_HGX_AUTOSCALE_NAME_PREFIX", "mac-fungible", environ=env
            ),
        )
        error = ""
        try:
            config.capacity_policy()
        except Exception as exc:  # noqa: BLE001 - expose configuration, do not crash app
            error = str(exc)
        if config.min_ready > config.max_sessions:
            error = "min_ready must not exceed max_sessions"
        if not error:
            return config
        return cls(**{**asdict(config), "configuration_error": error})


class HgxAutoscaler:
    """Reconcile sustained provisioning demand on a bounded background thread."""

    def __init__(
        self,
        control_plane: Any,
        config: HgxAutoscalerConfig,
        *,
        controller: Optional[HgxElasticCapacityController] = None,
        clock: Any = time.time,
    ) -> None:
        self.control_plane = control_plane
        self.config = config
        policy = (
            config.capacity_policy()
            if not config.configuration_error
            else HgxCapacityPolicy()
        )
        self.controller = controller or HgxElasticCapacityController(
            provider=HgxProvider(
                binary=config.hgx_binary,
                timeout=config.hgx_command_timeout_seconds,
            ),
            policy=policy,
            state_path=config.state_path,
            name_prefix=config.name_prefix,
        )
        self._clock = clock
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_report: Optional[Dict[str, Any]] = None
        self._zero_demand_since = float(clock())

    def start(self) -> bool:
        if not self.config.active:
            if self.config.configuration_error:
                self._observe(
                    "hgx.autoscaler.configuration_invalid",
                    "warning",
                    {"error": self.config.configuration_error},
                )
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._wake_event.clear()
            self.control_plane.provisioning.register_request_listener(
                self._on_provisioning_request
            )
            thread = threading.Thread(
                target=self._loop,
                name="mac-hgx-autoscaler",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        self._observe(
            "hgx.autoscaler.started", "info", {"config": self.config.to_dict()}
        )
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        self._wake_event.set()
        self.control_plane.provisioning.unregister_request_listener(
            self._on_provisioning_request
        )
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._observe("hgx.autoscaler.stopped", "info", {})
        return stopped

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            report = copy.deepcopy(self._last_report)
        return {
            "schema": AUTOSCALER_SCHEMA,
            "config": self.config.to_dict(),
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "run_active": self._run_lock.locked(),
            "last_report": report,
        }

    def _on_provisioning_request(
        self, _request: AgentProvisioningRequest
    ) -> None:
        self._wake_event.set()

    def _loop(self) -> None:
        if self._stop_event.wait(max(0.0, self.config.initial_delay_seconds)):
            return
        while not self._stop_event.is_set():
            try:
                self.run_once(trigger="scheduled")
            except Exception:  # noqa: BLE001 - a future cycle must still run
                _log.warning("HGX autoscaler cycle failed", exc_info=True)
            deadline = time.monotonic() + self.config.interval_seconds
            while not self._stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._wake_event.wait(timeout=remaining)
                self._wake_event.clear()

    def run_once(self, *, trigger: str = "operator") -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {
                "schema": AUTOSCALER_SCHEMA,
                "status": "busy",
                "trigger": trigger,
            }
        try:
            now = float(self._clock())
            pending = list(self.control_plane.provisioning.list_pending_requests(limit=1000))
            active, reconciled, ignored = self._active_requests(pending)
            sustained = [
                request
                for request in active
                if self._request_age_seconds(request, now)
                >= self.config.scale_up_stabilization_seconds
            ]
            raw_count = len(active)
            sustained_count = len(sustained)
            if raw_count:
                self._zero_demand_since = now
            zero_age = max(0.0, now - self._zero_demand_since)
            registered_agents = self._registered_agents()

            action = "observe"
            capacity_result: Optional[Dict[str, Any]] = None
            error_class = ""
            try:
                if sustained_count > 0 or self.config.min_ready > 0:
                    action = "scale_up_reconcile"
                    capacity_result = self.controller.execute(
                        pending_request_count=sustained_count,
                        registered_agents=registered_agents,
                    )
                elif (
                    raw_count == 0
                    and zero_age >= self.config.scale_down_stabilization_seconds
                ):
                    action = "scale_down_reconcile"
                    capacity_result = self.controller.retire_spare(
                        pending_request_count=0,
                        min_age_seconds=self.config.spare_min_age_seconds,
                        max_delete_count=self.config.scale_down_step,
                        registered_agents=registered_agents,
                    )
                elif raw_count:
                    action = "stabilizing_scale_up"
                else:
                    action = "stabilizing_scale_down"
            except HgxError as exc:
                error_class = exc.__class__.__name__
                action = "provider_error"
            except Exception as exc:  # noqa: BLE001 - preserve future cycles
                error_class = exc.__class__.__name__
                action = "controller_error"

            report = {
                "schema": AUTOSCALER_SCHEMA,
                "status": "error" if error_class else "ok",
                "trigger": trigger,
                "recorded_at": datetime.fromtimestamp(
                    now, tz=timezone.utc
                ).isoformat(),
                "action": action,
                "pending_request_count": raw_count,
                "sustained_pending_request_count": sustained_count,
                "reconciled_stale_request_ids": reconciled,
                "ignored_request_counts": ignored,
                "zero_demand_age_seconds": round(zero_age, 3),
                "error_class": error_class or None,
                "capacity": capacity_result,
            }
            with self._state_lock:
                self._last_report = report
            self._record_metrics(report)
            self._observe(
                "hgx.autoscaler.reconciled",
                "error" if error_class else "info",
                report,
            )
            return report
        finally:
            self._run_lock.release()

    def _active_requests(
        self, requests: List[AgentProvisioningRequest]
    ) -> tuple[List[AgentProvisioningRequest], List[str], Dict[str, int]]:
        active: List[AgentProvisioningRequest] = []
        reconciled: List[str] = []
        ignored: Dict[str, int] = {}
        for request in requests:
            if not request.task_id:
                if request.reason in TASK_BOUND_REQUEST_REASONS:
                    # Older dispatch/review paths emitted task-shaped demand
                    # without the task identity needed to prove that demand is
                    # still live.  Such rows cannot safely create compute and
                    # otherwise remain pending forever.
                    self.control_plane.provisioning.cancel_request(
                        request.id, reason="task-bound-request-missing-task-id"
                    )
                    reconciled.append(request.id)
                else:
                    ignored[request.reason] = ignored.get(request.reason, 0) + 1
                continue
            if request.reason not in HGX_SCALABLE_REQUEST_REASONS:
                # Reviewer and service-role shortages are valid provisioning
                # signals, but a generic coding worker cannot satisfy them.
                # Leave them pending for their matching provisioner while
                # excluding them from HGX capacity math.
                ignored[request.reason] = ignored.get(request.reason, 0) + 1
                continue
            try:
                task = self.control_plane.get_task(request.task_id)
            except NotFoundError:
                self.control_plane.provisioning.cancel_request(
                    request.id, reason="task-not-found"
                )
                reconciled.append(request.id)
                continue
            if task.state in TERMINAL_TASK_STATES:
                self.control_plane.provisioning.cancel_request(
                    request.id, reason="task-terminal"
                )
                reconciled.append(request.id)
                continue
            if request.reason == "dispatch.no_eligible_agent" and (
                task.state != TaskState.OPEN.value or task.owner_agent_id
            ):
                self.control_plane.provisioning.cancel_request(
                    request.id, reason="task-no-longer-awaiting-dispatch"
                )
                reconciled.append(request.id)
                continue
            if request.reason == "review.no_eligible_reviewer" and (
                task.state not in {TaskState.NEEDS_REVIEW.value}
            ):
                self.control_plane.provisioning.cancel_request(
                    request.id, reason="task-no-longer-awaiting-reviewer"
                )
                reconciled.append(request.id)
                continue
            active.append(request)
        return active, reconciled, ignored

    @staticmethod
    def _request_age_seconds(
        request: AgentProvisioningRequest, now: float
    ) -> float:
        try:
            created = datetime.fromisoformat(
                request.created_at.replace("Z", "+00:00")
            ).timestamp()
        except (AttributeError, ValueError):
            return 0.0
        return max(0.0, now - created)

    def _registered_agents(self) -> Optional[Any]:
        if not self.config.registered_agents_file:
            return None
        path = Path(self.config.registered_agents_file).expanduser()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HgxError(
                "registered HGX agent mapping is unavailable or invalid"
            ) from exc
        return value

    def _record_metrics(self, report: Mapping[str, Any]) -> None:
        for name, key in (
            ("hgx.autoscaler.pending_requests", "pending_request_count"),
            (
                "hgx.autoscaler.sustained_pending_requests",
                "sustained_pending_request_count",
            ),
            ("hgx.autoscaler.zero_demand_age_seconds", "zero_demand_age_seconds"),
        ):
            try:
                self.control_plane.record_metric(
                    name,
                    float(report.get(key) or 0),
                    unit="count" if key != "zero_demand_age_seconds" else "seconds",
                    layer="control_plane",
                    source="hgx-autoscaler",
                    detail={"action": report.get("action")},
                )
            except Exception:  # noqa: BLE001 - metrics cannot block capacity
                _log.warning("could not record HGX autoscaler metric", exc_info=True)
        ignored = report.get("ignored_request_counts")
        if isinstance(ignored, Mapping):
            try:
                self.control_plane.record_metric(
                    "hgx.autoscaler.ignored_requests",
                    float(sum(int(value) for value in ignored.values())),
                    unit="count",
                    layer="control_plane",
                    source="hgx-autoscaler",
                    detail={"reasons": dict(ignored)},
                )
            except Exception:  # noqa: BLE001 - metrics cannot block capacity
                _log.warning(
                    "could not record ignored HGX request metric", exc_info=True
                )

    def _observe(
        self, event_type: str, level: str, detail: Dict[str, Any]
    ) -> None:
        try:
            self.control_plane.record_log(
                event_type,
                layer="control_plane",
                source="hgx-autoscaler",
                level=level,
                subject_type="service",
                subject_id="hgx-autoscaler",
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - telemetry cannot block capacity
            _log.warning("could not record HGX autoscaler event", exc_info=True)


__all__ = [
    "AUTOSCALER_SCHEMA",
    "HgxAutoscaler",
    "HgxAutoscalerConfig",
]
