"""Bounded asynchronous controller for the work-package assembly line.

The work-package services deliberately own their individual durable state
machines.  This module does not add a second lifecycle database or a global
barrier.  It observes independently ready integration groups and advances at
most one durable station for each observed item:

``accepted -> integration -> certification -> acceptance -> land -> finalize``

Every mutating operation remains fenced/idempotent in the station service that
owns it.  The controller supplies bounded scheduling, downstream release
gating, failure isolation, and a stoppable background clock.  Request handlers
should call :meth:`trigger`, which only wakes the controller thread; they must
not run external certification or Git assembly inline.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from mac.gitops import redact_git_remote_auth_in_text


WORK_PACKAGE_PIPELINE_SCHEMA = "mac.work_package.pipeline.v1"
WORK_PACKAGE_PIPELINE_RUN_SCHEMA = "mac.work_package.pipeline_run.v1"
WORK_PACKAGE_PIPELINE_OUTCOME_SCHEMA = "mac.work_package.pipeline_outcome.v1"

# Durable resume-cursor coordinates for the bounded controller. The scan
# bookmark (``after_key``) is otherwise restart-losable: a hub restart would
# rewind to an empty key and rescan the whole catalog. Persisting it under a
# stable (scope, name) lets the controller resume where it stopped.
WORK_PACKAGE_PIPELINE_CURSOR_SCOPE = "work_package_pipeline"
WORK_PACKAGE_PIPELINE_AFTER_KEY_CURSOR = "after_key"


class PipelineCursorStore(Protocol):
    """Minimal durable key/value seam for the controller's resume cursor.

    Satisfied by :class:`mac.store.SQLiteStore` (and the Postgres port) via
    ``get_pipeline_cursor`` / ``set_pipeline_cursor``; injected so the
    controller has no direct dependency on a concrete store.
    """

    def get_pipeline_cursor(self, scope: str, name: str, default: Any = ...) -> Any: ...

    def set_pipeline_cursor(self, scope: str, name: str, value: Any) -> None: ...


_ACTIONABLE_BATCH_STATES = frozenset(
    {"queued", "assembling", "verifying", "certified", "rejected", "published"}
)
_TERMINAL_BATCH_STATES = frozenset({"stale", "cancelled"})
_RUNNABLE_JOB_STATES = frozenset({"queued", "running"})
_TERMINAL_JOB_STATES = frozenset({"completed", "failed"})
_SECRET_FIELD_RE = re.compile(
    r"(?i)\b(token|password|passwd|secret|authorization|cookie|api[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")

_log = logging.getLogger("mac.work_package_pipeline")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _safe_error(error: Any, limit: int) -> str:
    """Return a bounded diagnostic with common credential shapes removed."""

    text = redact_git_remote_auth_in_text(str(error or "")).replace("\x00", " ")
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _SECRET_FIELD_RE.sub(r"\1\2<redacted>", text)
    text = " ".join(text.split())
    return text[: max(32, int(limit))]


def _public(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return dict(converted)
    if hasattr(value, "__dataclass_fields__"):
        converted = asdict(value)
        if isinstance(converted, dict):
            return converted
    return {}


def _bounded_identity(value: Any) -> str:
    return str(value or "").strip()[:256]


@dataclass(frozen=True)
class WorkPackagePipelineConfig:
    """Runtime bounds for one controller instance.

    ``enabled`` is false by default.  A deployment must explicitly opt in after
    it has provided both a certification contract and an enabled landing
    endpoint through the release gate.
    """

    enabled: bool = False
    interval_seconds: float = 10.0
    initial_delay_seconds: float = 5.0
    max_actions_per_run: int = 8
    max_items_per_run: int = 128
    max_error_chars: int = 500
    actor: str = "work-package-pipeline-controller"

    def __post_init__(self) -> None:
        if not 0.05 <= float(self.interval_seconds) <= 86_400:
            raise ValueError("pipeline interval_seconds must be between 0.05 and 86400")
        if not 0 <= float(self.initial_delay_seconds) <= 86_400:
            raise ValueError(
                "pipeline initial_delay_seconds must be between 0 and 86400"
            )
        if not 1 <= int(self.max_actions_per_run) <= 1_000:
            raise ValueError("pipeline max_actions_per_run must be between 1 and 1000")
        if not int(self.max_actions_per_run) <= int(self.max_items_per_run) <= 10_000:
            raise ValueError(
                "pipeline max_items_per_run must be between max_actions_per_run and 10000"
            )
        if not 64 <= int(self.max_error_chars) <= 2_000:
            raise ValueError("pipeline max_error_chars must be between 64 and 2000")
        if not str(self.actor or "").strip():
            raise ValueError("pipeline actor is required")

    @classmethod
    def from_env(cls) -> "WorkPackagePipelineConfig":
        def enabled(name: str, default: bool) -> bool:
            raw = os.environ.get(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        def number(name: str, default: float) -> float:
            raw = str(os.environ.get(name) or "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        return cls(
            enabled=enabled("MAC_WORK_PACKAGE_PIPELINE_ENABLED", False),
            interval_seconds=max(
                0.05,
                min(
                    86_400,
                    number("MAC_WORK_PACKAGE_PIPELINE_INTERVAL_SECONDS", 10.0),
                ),
            ),
            initial_delay_seconds=max(
                0.0,
                min(
                    86_400,
                    number("MAC_WORK_PACKAGE_PIPELINE_INITIAL_DELAY_SECONDS", 5.0),
                ),
            ),
            max_actions_per_run=max(
                1,
                min(
                    1_000,
                    int(number("MAC_WORK_PACKAGE_PIPELINE_MAX_ACTIONS", 8)),
                ),
            ),
            max_items_per_run=max(
                max(
                    1,
                    min(
                        1_000,
                        int(number("MAC_WORK_PACKAGE_PIPELINE_MAX_ACTIONS", 8)),
                    ),
                ),
                min(
                    10_000,
                    int(number("MAC_WORK_PACKAGE_PIPELINE_MAX_ITEMS", 128)),
                ),
            ),
            max_error_chars=max(
                64,
                min(
                    2_000,
                    int(number("MAC_WORK_PACKAGE_PIPELINE_MAX_ERROR_CHARS", 500)),
                ),
            ),
            actor=(
                os.environ.get("MAC_WORK_PACKAGE_PIPELINE_ACTOR")
                or "work-package-pipeline-controller"
            ).strip(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "initial_delay_seconds": self.initial_delay_seconds,
            "max_actions_per_run": self.max_actions_per_run,
            "max_items_per_run": self.max_items_per_run,
            "max_error_chars": self.max_error_chars,
            "actor": self.actor,
        }


@dataclass(frozen=True)
class PipelineSnapshot:
    """One independently actionable integration group at an observed instant."""

    key: str
    package_id: str
    plan_version: int
    epoch: int
    integration_node_key: str
    integration_task_id: str = ""
    integration_node_state: str = ""
    certification_node_key: str = ""
    certification_task_id: str = ""
    certification_node_state: str = ""
    batch_id: str = ""
    batch_state: str = ""
    certification_job_id: str = ""
    certification_job_state: str = ""
    certification_id: str = ""
    product_finalized: bool = False
    rejection_finalized: bool = False
    blocker: str = ""

    def __post_init__(self) -> None:
        required = (self.key, self.package_id, self.integration_node_key)
        if any(not str(value or "").strip() for value in required):
            raise ValueError(
                "pipeline snapshot key, package, and integration node are required"
            )
        if int(self.plan_version) < 1 or int(self.epoch) < 1:
            raise ValueError(
                "pipeline snapshot plan version and epoch must be positive"
            )
        if self.batch_state and not self.batch_id:
            raise ValueError("pipeline snapshot batch state requires a batch id")
        if self.certification_job_state and not self.certification_job_id:
            raise ValueError("pipeline certification job state requires a job id")


@dataclass(frozen=True)
class PipelineReleaseGate:
    """Downstream pull signal for one exact work item.

    ``endpoint`` is intentionally opaque and never serialized into telemetry.
    It may carry a credential *source name* but must not carry credential values.
    """

    certification_contract_ready: bool
    landing_enabled: bool
    endpoint: Any = None
    reason: str = ""

    @property
    def ready(self) -> bool:
        return bool(
            self.certification_contract_ready
            and self.landing_enabled
            and self.endpoint is not None
        )

    @property
    def code(self) -> str:
        if not self.certification_contract_ready:
            return "certification_contract_unavailable"
        if not self.landing_enabled:
            return "landing_disabled"
        if self.endpoint is None:
            return "landing_endpoint_unavailable"
        return "ready"


class PipelineInventory(Protocol):
    def discover(self, *, after_key: str, limit: int) -> Sequence[PipelineSnapshot]: ...


class PipelineReleaseGateResolver(Protocol):
    def resolve(self, snapshot: PipelineSnapshot) -> PipelineReleaseGate: ...


class CertificationBundleProvider(Protocol):
    def ensure_bundle(self, snapshot: PipelineSnapshot) -> Path: ...


class IntegrationStation(Protocol):
    def create_batch(
        self, package_id: str, integration_node_key: str, *, actor: str
    ) -> Any: ...

    def assemble(self, batch_id: str) -> Any: ...


class CertificationStation(Protocol):
    def prepare(self, batch_id: str, bundle_path: Path, *, actor: str) -> Any: ...

    def run(
        self,
        job_id: str,
        bundle_path: Path,
        *,
        owner: Optional[str] = None,
    ) -> Any: ...


class LandingStation(Protocol):
    def accept_certification(
        self,
        batch_id: str,
        endpoint: Any,
        *,
        certification_id: str,
    ) -> Any: ...

    def land(self, batch_id: str, endpoint: Any) -> Any: ...


class ProductFinalizationStation(Protocol):
    def finalize_landed_batch(self, batch_id: str, *, actor: str) -> Any:
        """Atomically consume a landing receipt and release product WIP.

        The implementation must validate the exact append-only landing receipt,
        advance the integration task and package-node projections, release the
        batch's held integration WIP, and complete the package when every node
        is final.  It must be idempotent as a unit; the controller intentionally
        does not reproduce those coupled database writes.
        """

        ...


class ProductRejectionStation(Protocol):
    def reject_failed_certification(
        self,
        batch_id: str,
        *,
        certification_id: str,
        actor: str,
    ) -> Any:
        """Atomically dispose a failed exact product without leaking WIP.

        The implementation must validate the failed certification and exact
        integrated/rejected controller-node provenance, reject the batch,
        pause the package with a scoped Andon record, and return or quarantine
        every held integration WIP token.  Retries must be idempotent.
        """

        ...


@dataclass(frozen=True)
class PipelineOutcome:
    item_key: str
    package_id: str
    plan_version: int
    epoch: int
    station: str
    status: str
    attempted: bool
    batch_id: str = ""
    job_id: str = ""
    code: str = ""
    started_at: str = ""
    completed_at: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": WORK_PACKAGE_PIPELINE_OUTCOME_SCHEMA,
            "item_key": self.item_key,
            "package_id": self.package_id,
            "plan_version": self.plan_version,
            "epoch": self.epoch,
            "batch_id": self.batch_id,
            "job_id": self.job_id,
            "station": self.station,
            "status": self.status,
            "attempted": self.attempted,
            "code": self.code,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class PipelineRunReport:
    run_id: str
    trigger: str
    status: str
    started_at: str
    completed_at: str
    scanned_count: int
    action_count: int
    next_after_key: str
    outcomes: Tuple[PipelineOutcome, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": WORK_PACKAGE_PIPELINE_RUN_SCHEMA,
            "run_id": self.run_id,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "scanned_count": self.scanned_count,
            "action_count": self.action_count,
            "next_after_key": self.next_after_key,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


PipelineObserver = Callable[[Mapping[str, Any]], None]


def control_plane_pipeline_observer(control_plane: Any) -> PipelineObserver:
    """Adapt ``ControlPlane.record_log`` to durable bounded run telemetry."""

    def observe(report: Mapping[str, Any]) -> None:
        status = str(report.get("status") or "")
        level = (
            "warning"
            if status in {"failed", "partial_failure", "completed_with_contention"}
            else "info"
        )
        try:
            control_plane.record_log(
                "work_package.pipeline.run",
                layer="control_plane",
                source="work-package-pipeline",
                level=level,
                subject_type="service",
                subject_id="work-package-pipeline",
                detail=dict(report),
            )
        except Exception:  # noqa: BLE001 - measurement must still be attempted.
            _log.warning(
                "work-package pipeline ordinary log write failed", exc_info=True
            )
        recorder = getattr(
            control_plane, "record_work_package_pipeline_telemetry", None
        )
        if callable(recorder):
            try:
                recorder(report)
            except Exception as exc:  # noqa: BLE001 - controller progress is isolated.
                failure_recorder = getattr(
                    control_plane, "record_work_package_telemetry_failure", None
                )
                if callable(failure_recorder):
                    try:
                        failure_recorder("pipeline_report", exc)
                    except Exception:  # noqa: BLE001 - no recursive telemetry.
                        _log.error(
                            "work-package telemetry failure counter write failed",
                            exc_info=True,
                        )
                _log.error(
                    "work-package pipeline telemetry write failed", exc_info=True
                )
            else:
                success_recorder = getattr(
                    control_plane, "record_work_package_telemetry_success", None
                )
                if callable(success_recorder):
                    try:
                        success_recorder()
                    except Exception:  # noqa: BLE001 - health is best effort only.
                        _log.warning(
                            "work-package telemetry success marker write failed",
                            exc_info=True,
                        )

    return observe


class WorkPackagePipelineController:
    """Advance independent work-package stations under a bounded local clock."""

    def __init__(
        self,
        *,
        inventory: PipelineInventory,
        release_gates: PipelineReleaseGateResolver,
        bundles: CertificationBundleProvider,
        integration: IntegrationStation,
        certification: CertificationStation,
        landing: LandingStation,
        finalization: ProductFinalizationStation,
        rejection: ProductRejectionStation,
        config: Optional[WorkPackagePipelineConfig] = None,
        observer: Optional[PipelineObserver] = None,
        owner: Optional[str] = None,
        now: Optional[Callable[[], datetime]] = None,
        cursor_store: Optional[PipelineCursorStore] = None,
    ) -> None:
        self.inventory = inventory
        self.release_gates = release_gates
        self.bundles = bundles
        self.integration = integration
        self.certification = certification
        self.landing = landing
        self.finalization = finalization
        self.rejection = rejection
        self.config = config or WorkPackagePipelineConfig()
        self.observer = observer or (lambda _report: None)
        self.owner = str(owner or "work-package-pipeline-%s" % uuid.uuid4().hex).strip()
        if not self.owner:
            raise ValueError("pipeline controller owner is required")
        self._now = now or _utcnow
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_report: Optional[Dict[str, Any]] = None
        self._cursor_store = cursor_store
        self._after_key = self._load_after_key()

    def _load_after_key(self) -> str:
        """Read the durable scan bookmark, defaulting to empty on any error."""

        store = self._cursor_store
        if store is None:
            return ""
        try:
            value = store.get_pipeline_cursor(
                WORK_PACKAGE_PIPELINE_CURSOR_SCOPE,
                WORK_PACKAGE_PIPELINE_AFTER_KEY_CURSOR,
                "",
            )
        except Exception:  # noqa: BLE001 - a cursor read must never crash boot.
            _log.warning("failed to load work-package pipeline cursor", exc_info=True)
            return ""
        return str(value or "")

    def _persist_after_key(self, after_key: str) -> None:
        """Best-effort durable write of the scan bookmark.

        Persistence failures are logged and swallowed: losing a bookmark write
        only costs a re-scan on the next restart, so it must never abort a run.
        """

        store = self._cursor_store
        if store is None:
            return
        try:
            store.set_pipeline_cursor(
                WORK_PACKAGE_PIPELINE_CURSOR_SCOPE,
                WORK_PACKAGE_PIPELINE_AFTER_KEY_CURSOR,
                str(after_key or ""),
            )
        except Exception:  # noqa: BLE001 - never fail a run on a cursor write.
            _log.warning("failed to persist work-package pipeline cursor", exc_info=True)

    def start(self) -> bool:
        """Start a stoppable daemon; disabled configurations fail closed."""

        if not self.config.enabled:
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._wake_event.clear()
            thread = threading.Thread(
                target=self._loop,
                name="mac-work-package-pipeline",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        self._wake_event.set()
        with self._state_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        return thread is None or not thread.is_alive()

    def trigger(self) -> bool:
        """Wake the background controller without doing station work inline."""

        with self._state_lock:
            thread = self._thread
            running = bool(thread is not None and thread.is_alive())
        if running:
            self._wake_event.set()
        return running

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            thread = self._thread
            last_report = copy.deepcopy(self._last_report)
            after_key = self._after_key
        return {
            "schema": WORK_PACKAGE_PIPELINE_SCHEMA,
            "config": self.config.to_dict(),
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "run_active": self._run_lock.locked(),
            "next_after_key": after_key,
            "last_report": last_report,
        }

    def run_once(self, *, trigger: str = "operator") -> PipelineRunReport:
        """Run one bounded pass.

        This method is intended for the controller thread and tests.  HTTP
        handlers should use :meth:`trigger` so Git and OpenShell never occupy a
        request thread.
        """

        started = self._now_utc()
        run_id = "wppipe_%s" % uuid.uuid4().hex
        trigger_value = _bounded_identity(trigger) or "operator"
        if not self._run_lock.acquire(blocking=False):
            report = PipelineRunReport(
                run_id=run_id,
                trigger=trigger_value,
                status="busy",
                started_at=_iso(started),
                completed_at=_iso(self._now_utc()),
                scanned_count=0,
                action_count=0,
                next_after_key=self._after_key,
                outcomes=(),
            )
            self._finish(report)
            return report

        outcomes: list[PipelineOutcome] = []
        scanned = 0
        actions = 0
        try:
            try:
                snapshots = tuple(
                    self.inventory.discover(
                        after_key=self._after_key,
                        limit=int(self.config.max_items_per_run),
                    )
                )[: int(self.config.max_items_per_run)]
            except Exception as exc:  # noqa: BLE001 - retry on a future pass.
                outcomes.append(
                    PipelineOutcome(
                        item_key="inventory",
                        package_id="",
                        plan_version=0,
                        epoch=0,
                        station="inventory",
                        status="failed",
                        attempted=False,
                        code="inventory_failed",
                        detail={"error": self._error(exc)},
                    )
                )
                return self._complete_report(
                    run_id,
                    trigger_value,
                    started,
                    scanned,
                    actions,
                    outcomes,
                )

            for snapshot in snapshots:
                if actions >= int(self.config.max_actions_per_run):
                    break
                scanned += 1
                with self._state_lock:
                    self._after_key = snapshot.key
                self._persist_after_key(snapshot.key)
                action_started = self._now_utc()
                outcome = self._advance(snapshot)
                action_completed = self._now_utc()
                outcome = replace(
                    outcome,
                    started_at=_iso(action_started),
                    completed_at=_iso(action_completed),
                )
                outcomes.append(outcome)
                if outcome.attempted:
                    actions += 1
            return self._complete_report(
                run_id,
                trigger_value,
                started,
                scanned,
                actions,
                outcomes,
            )
        finally:
            self._run_lock.release()

    def _advance(self, snapshot: PipelineSnapshot) -> PipelineOutcome:
        if snapshot.blocker:
            return self._outcome(
                snapshot,
                "inventory",
                "deferred",
                False,
                "inventory_invariant_blocked",
                reason=self._safe_reason(snapshot.blocker),
            )
        provenance_blocker = self._provenance_blocker(snapshot)
        if provenance_blocker:
            return self._outcome(
                snapshot,
                "controller_provenance",
                "deferred",
                False,
                "controller_provenance_unready",
                reason=provenance_blocker,
            )
        # Publication is not product completion.  Once the append-only landing
        # receipt exists, release WIP and advance task/node/package projections
        # even if an operator subsequently disables new certification/landing.
        # That coupled commit belongs to the dedicated durable finalizer.
        if snapshot.batch_state == "published":
            if snapshot.product_finalized:
                return self._outcome(
                    snapshot,
                    "product_finalization",
                    "no_op",
                    False,
                    "product_already_finalized",
                )
            return self._finalize_product(snapshot)
        if snapshot.batch_state == "rejected":
            if snapshot.rejection_finalized:
                return self._outcome(
                    snapshot,
                    "certification_rejection",
                    "no_op",
                    False,
                    "rejection_already_finalized",
                )
            if not snapshot.certification_id:
                return self._outcome(
                    snapshot,
                    "certification_rejection",
                    "failed",
                    False,
                    "rejected_batch_missing_certification",
                )
            return self._reject_certification(snapshot)
        try:
            gate = self.release_gates.resolve(snapshot)
        except Exception as exc:  # noqa: BLE001 - isolate one package.
            return self._failure(snapshot, "release_gate", exc, attempted=False)
        if not isinstance(gate, PipelineReleaseGate):
            return self._outcome(
                snapshot,
                "release_gate",
                "failed",
                False,
                "release_gate_malformed",
            )
        if not gate.ready:
            return self._outcome(
                snapshot,
                "release_gate",
                "deferred",
                False,
                gate.code,
                reason=self._safe_reason(gate.reason),
            )

        if not snapshot.batch_id:
            return self._call(
                snapshot,
                "integration_batch",
                lambda: self.integration.create_batch(
                    snapshot.package_id,
                    snapshot.integration_node_key,
                    actor=self.config.actor,
                ),
            )
        if snapshot.batch_state in {"queued", "assembling"}:
            return self._call(
                snapshot,
                "integration_assembly",
                lambda: self.integration.assemble(snapshot.batch_id),
            )
        if snapshot.batch_state == "verifying":
            return self._advance_certification(snapshot, gate)
        if snapshot.batch_state == "certified":
            return self._call(
                snapshot,
                "landing",
                lambda: self.landing.land(snapshot.batch_id, gate.endpoint),
            )
        if snapshot.batch_state in _TERMINAL_BATCH_STATES:
            return self._outcome(
                snapshot,
                "complete",
                "no_op",
                False,
                "batch_terminal",
            )
        return self._outcome(
            snapshot,
            "inventory",
            "failed",
            False,
            "unknown_batch_state",
            state=_bounded_identity(snapshot.batch_state),
        )

    def _provenance_blocker(self, snapshot: PipelineSnapshot) -> str:
        if not snapshot.integration_task_id:
            return "exact integration controller task/link is not ready"
        if not snapshot.batch_id:
            if snapshot.integration_node_state != "ready":
                return "exact integration controller task/link is not ready"
            return ""
        if snapshot.batch_state in {"queued", "assembling"}:
            if snapshot.integration_node_state not in {"ready", "executing"}:
                return "integration controller node is not assembling"
            return ""
        if snapshot.batch_state in {"verifying", "certified", "published"}:
            if snapshot.integration_node_state != "integrated":
                return "assembled batch lacks exact integrated-node provenance"
            if (
                not snapshot.certification_node_key
                or not snapshot.certification_task_id
            ):
                return "assembled batch has no exact certification controller node"
        if snapshot.batch_state == "verifying":
            if not snapshot.certification_job_id or (
                snapshot.certification_job_state in _RUNNABLE_JOB_STATES
            ):
                expected = "ready"
            elif snapshot.certification_job_state == "completed":
                expected = "certified"
            elif snapshot.certification_job_state == "failed":
                expected = "rejected"
            else:
                expected = ""
            if expected and snapshot.certification_node_state != expected:
                return "certification controller node is not %s" % expected
        if (
            snapshot.batch_state in {"certified", "published"}
            and snapshot.certification_node_state != "certified"
        ):
            return "release lacks exact certified-node provenance"
        if snapshot.batch_state == "rejected":
            if snapshot.integration_node_state != "integrated":
                return "rejected batch lacks exact integrated-node provenance"
            if (
                snapshot.certification_node_state != "rejected"
                or snapshot.certification_job_state != "failed"
                or not snapshot.certification_id
            ):
                return "rejected batch lacks exact failed-certification provenance"
        return ""

    def _advance_certification(
        self,
        snapshot: PipelineSnapshot,
        gate: PipelineReleaseGate,
    ) -> PipelineOutcome:
        if not snapshot.certification_job_id:
            return self._call_with_bundle(
                snapshot,
                "certification_prepare",
                lambda bundle: self.certification.prepare(
                    snapshot.batch_id,
                    bundle,
                    actor=self.config.actor,
                ),
            )
        if snapshot.certification_job_state in _RUNNABLE_JOB_STATES:
            return self._run_certification(snapshot)
        if snapshot.certification_job_state in _TERMINAL_JOB_STATES:
            if not snapshot.certification_id:
                return self._outcome(
                    snapshot,
                    "certification_acceptance",
                    "failed",
                    False,
                    "terminal_job_missing_certification",
                )
            if snapshot.certification_job_state == "failed":
                return self._reject_certification(snapshot)
            return self._call(
                snapshot,
                "certification_acceptance",
                lambda: self.landing.accept_certification(
                    snapshot.batch_id,
                    gate.endpoint,
                    certification_id=snapshot.certification_id,
                ),
            )
        return self._outcome(
            snapshot,
            "certification",
            "failed",
            False,
            "unknown_certification_job_state",
            state=_bounded_identity(snapshot.certification_job_state),
        )

    def _run_certification(self, snapshot: PipelineSnapshot) -> PipelineOutcome:
        station = "certification_run"
        try:
            bundle = Path(self.bundles.ensure_bundle(snapshot))
            if not bundle.is_file():
                raise RuntimeError("certification bundle is unavailable")
            result = self.certification.run(
                snapshot.certification_job_id,
                bundle,
                owner=self.owner,
            )
        except Exception as exc:  # noqa: BLE001 - one station cannot stop peers.
            return self._failure(snapshot, station, exc, attempted=True)
        value = _public(result)
        if value.get("status") == "failed":
            certification_id = str(value.get("certification_id") or "").strip()
            if not certification_id:
                return self._outcome(
                    snapshot,
                    station,
                    "failed",
                    True,
                    "failed_certification_missing_rejection_receipt",
                )
            # Failed ingestion atomically rejects/pauses/disposes WIP.  Validate
            # its durable readback now because the package is no longer active
            # and therefore will not appear in the next normal inventory pass.
            rejected = replace(
                snapshot,
                batch_state="rejected",
                certification_job_state="failed",
                certification_id=certification_id,
                certification_node_state="rejected",
            )
            return self._reject_certification(rejected)
        return self._success(snapshot, station, value)

    def _finalize_product(self, snapshot: PipelineSnapshot) -> PipelineOutcome:
        station = "product_finalization"
        try:
            result = self.finalization.finalize_landed_batch(
                snapshot.batch_id,
                actor=self.config.actor,
            )
        except Exception as exc:  # noqa: BLE001 - one station cannot stop peers.
            return self._failure(snapshot, station, exc, attempted=True)
        value = _public(result)
        valid = bool(
            value.get("status") == "completed"
            and value.get("batch_state") == "published"
            and str(value.get("landing_receipt_id") or "").strip()
            and value.get("provenance_verified") is True
            and value.get("integration_task_id") == snapshot.integration_task_id
            and value.get("certification_task_id") == snapshot.certification_task_id
            and value.get("integration_node_state") == "integrated"
            and value.get("certification_node_state") == "certified"
            and value.get("integration_task_completed") is True
            and value.get("held_wip_count") == 0
            and value.get("package_state") in {"active", "completed"}
        )
        if not valid:
            return self._outcome(
                snapshot,
                station,
                "failed",
                True,
                "product_finalization_receipt_incomplete",
            )
        return self._success(snapshot, station, value)

    def _reject_certification(self, snapshot: PipelineSnapshot) -> PipelineOutcome:
        station = "certification_rejection"
        try:
            result = self.rejection.reject_failed_certification(
                snapshot.batch_id,
                certification_id=snapshot.certification_id,
                actor=self.config.actor,
            )
        except Exception as exc:  # noqa: BLE001 - one station cannot stop peers.
            return self._failure(snapshot, station, exc, attempted=True)
        value = _public(result)
        valid = bool(
            value.get("status") == "completed"
            and value.get("batch_state") == "rejected"
            and value.get("certification_id") == snapshot.certification_id
            and value.get("provenance_verified") is True
            and value.get("integration_task_id") == snapshot.integration_task_id
            and value.get("certification_task_id") == snapshot.certification_task_id
            and value.get("integration_node_state") == "integrated"
            and value.get("certification_node_state") == "rejected"
            and value.get("andon_recorded") is True
            and value.get("package_state") == "paused"
            and value.get("wip_disposition") in {"returned", "quarantined"}
            and value.get("held_wip_count") == 0
        )
        if not valid:
            return self._outcome(
                snapshot,
                station,
                "failed",
                True,
                "certification_rejection_receipt_incomplete",
            )
        return self._success(snapshot, station, value)

    def _call_with_bundle(
        self,
        snapshot: PipelineSnapshot,
        station: str,
        operation: Callable[[Path], Any],
    ) -> PipelineOutcome:
        try:
            bundle = Path(self.bundles.ensure_bundle(snapshot))
            if not bundle.is_file():
                raise RuntimeError("certification bundle is unavailable")
            result = operation(bundle)
        except Exception as exc:  # noqa: BLE001 - one station cannot stop peers.
            return self._failure(snapshot, station, exc, attempted=True)
        return self._success(snapshot, station, result)

    def _call(
        self,
        snapshot: PipelineSnapshot,
        station: str,
        operation: Callable[[], Any],
    ) -> PipelineOutcome:
        try:
            result = operation()
        except Exception as exc:  # noqa: BLE001 - one station cannot stop peers.
            return self._failure(snapshot, station, exc, attempted=True)
        return self._success(snapshot, station, result)

    def _success(
        self, snapshot: PipelineSnapshot, station: str, result: Any
    ) -> PipelineOutcome:
        value = _public(result)
        detail: Dict[str, Any] = {}
        for name in ("status", "created", "recovered"):
            if name in value and isinstance(value[name], (str, bool, int)):
                observed = value[name]
                if isinstance(observed, str):
                    observed = _bounded_identity(observed)
                detail["station_%s" % name if name == "status" else name] = observed
        outcome = self._outcome(
            snapshot,
            station,
            "advanced",
            True,
            "station_advanced",
            **detail,
        )
        job_id = value.get("job_id") or value.get("certification_job_id")
        if station == "certification_prepare":
            # Certification preparation returns its durable job identity as
            # ``id``.  Preserve it on the same station outcome so telemetry
            # can use the job's immutable creation time as the queue boundary
            # without waiting for a later inventory pass.
            job_id = job_id or value.get("id")
        return replace(
            outcome,
            batch_id=_bounded_identity(value.get("batch_id") or outcome.batch_id),
            job_id=_bounded_identity(job_id or outcome.job_id),
        )

    def _failure(
        self,
        snapshot: PipelineSnapshot,
        station: str,
        error: Exception,
        *,
        attempted: bool,
    ) -> PipelineOutcome:
        name = type(error).__name__
        retryable = "Busy" in name or "LeaseLost" in name
        return self._outcome(
            snapshot,
            station,
            "busy" if retryable else "failed",
            attempted,
            "station_busy" if retryable else "station_failed",
            error_type=_bounded_identity(name),
            error=self._error(error),
        )

    def _outcome(
        self,
        snapshot: PipelineSnapshot,
        station: str,
        status: str,
        attempted: bool,
        code: str,
        **detail: Any,
    ) -> PipelineOutcome:
        return PipelineOutcome(
            item_key=_bounded_identity(snapshot.key),
            package_id=_bounded_identity(snapshot.package_id),
            plan_version=int(snapshot.plan_version),
            epoch=int(snapshot.epoch),
            batch_id=_bounded_identity(snapshot.batch_id),
            job_id=_bounded_identity(snapshot.certification_job_id),
            station=station,
            status=status,
            attempted=attempted,
            code=code,
            detail=detail,
        )

    def _complete_report(
        self,
        run_id: str,
        trigger: str,
        started: datetime,
        scanned: int,
        actions: int,
        outcomes: Sequence[PipelineOutcome],
    ) -> PipelineRunReport:
        pruner = getattr(self.bundles, "prune", None)
        if callable(pruner):
            try:
                pruner()
            except Exception:  # noqa: BLE001 - rebuildable cache maintenance is isolated.
                _log.warning(
                    "could not prune certification bundle cache", exc_info=True
                )
        statuses = {item.status for item in outcomes}
        if "failed" in statuses:
            status = "partial_failure" if len(outcomes) > 1 else "failed"
        elif "busy" in statuses:
            status = "completed_with_contention"
        elif outcomes and statuses <= {"deferred", "no_op"}:
            status = "blocked"
        else:
            status = "completed"
        report = PipelineRunReport(
            run_id=run_id,
            trigger=trigger,
            status=status,
            started_at=_iso(started),
            completed_at=_iso(self._now_utc()),
            scanned_count=scanned,
            action_count=actions,
            next_after_key=self._after_key,
            outcomes=tuple(outcomes[: int(self.config.max_items_per_run)]),
        )
        self._finish(report)
        return report

    def _finish(self, report: PipelineRunReport) -> None:
        value = report.to_dict()
        with self._state_lock:
            self._last_report = copy.deepcopy(value)
        try:
            self.observer(value)
        except Exception:  # noqa: BLE001 - telemetry cannot stop the line.
            _log.warning(
                "could not record work-package pipeline telemetry", exc_info=True
            )

    def _loop(self) -> None:
        if self._wait(float(self.config.initial_delay_seconds)):
            return
        while not self._stop_event.is_set():
            try:
                self.run_once(trigger="scheduled")
            except Exception:  # noqa: BLE001 - a future pass must remain possible.
                _log.warning("work-package pipeline tick failed", exc_info=True)
            if self._wait(float(self.config.interval_seconds)):
                return

    def _wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while not self._stop_event.is_set():
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                return False
            if self._wake_event.wait(remaining):
                self._wake_event.clear()
                return self._stop_event.is_set()
        return True

    def _now_utc(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _error(self, error: Any) -> str:
        return _safe_error(error, int(self.config.max_error_chars))

    def _safe_reason(self, reason: Any) -> str:
        return _safe_error(reason, min(256, int(self.config.max_error_chars)))


class ServicePipelineInventory:
    """Build snapshots from public work-package and certification projections.

    Callables keep this adapter independent of ``ControlPlane`` composition and
    let hub wiring select local services without importing API or store schema.
    The compiled plan remains the authority for integration membership; the
    adapter never infers a global epoch barrier across packages.
    """

    def __init__(
        self,
        *,
        list_packages: Callable[..., Sequence[Any]],
        describe_package: Callable[[str], Mapping[str, Any]],
        list_certification_jobs: Callable[..., Sequence[Any]],
        catalog_limit: int = 1_000,
        paged_catalog: bool = False,
    ) -> None:
        self._list_packages = list_packages
        self._describe_package = describe_package
        self._list_jobs = list_certification_jobs
        self._catalog_limit = max(1, min(int(catalog_limit), 10_000))
        self._paged_catalog = bool(paged_catalog)

    def discover(self, *, after_key: str, limit: int) -> Sequence[PipelineSnapshot]:
        limit_value = max(1, min(int(limit), self._catalog_limit))
        snapshots: list[PipelineSnapshot] = []
        cursor = str(after_key or "")
        packages = self._package_catalog(cursor=cursor, limit=limit_value)
        for raw_package in packages:
            package = _public(raw_package)
            package_id = str(package.get("id") or "").strip()
            if not package_id:
                continue
            described = dict(self._describe_package(package_id))
            projected = _public(described.get("package")) or package
            plan_version = int(projected.get("current_plan_version") or 0)
            epoch = int(projected.get("current_epoch") or 0)
            if plan_version < 1 or epoch < 1:
                if self._paged_catalog:
                    snapshots.append(
                        self._catalog_marker(
                            package_id,
                            max(1, plan_version),
                            max(1, epoch),
                            "active package has invalid plan-version or epoch projection",
                        )
                    )
                continue
            plan = _public(described.get("plan"))
            definition = _public(plan.get("definition"))
            derived = _public(definition.get("derived"))
            groups = derived.get("integration_groups")
            if not isinstance(groups, list):
                groups = []
            compiled_nodes = {
                str(node.get("node_key") or ""): dict(node)
                for node in definition.get("nodes") or []
                if isinstance(node, Mapping)
            }
            nodes = {
                str(node.get("node_key") or ""): dict(node)
                for node in described.get("nodes") or []
                if isinstance(node, Mapping)
            }
            batches = [
                _public(batch)
                for batch in described.get("batches") or []
                if isinstance(batch, Mapping)
                and int(batch.get("plan_version") or 0) == plan_version
                and int(batch.get("epoch") or 0) == epoch
            ]
            batch_ids = tuple(
                sorted(
                    str(batch.get("id") or "").strip()
                    for batch in batches
                    if str(batch.get("id") or "").strip()
                )
            )
            jobs_by_batch: Dict[str, list[Dict[str, Any]]] = {}
            for raw in self._list_jobs(
                state=None,
                batch_ids=batch_ids,
                limit=max(1, min(self._catalog_limit, len(batch_ids) or 1)),
            ):
                job = _public(raw)
                batch_id = str(job.get("batch_id") or "").strip()
                if batch_id in batch_ids:
                    jobs_by_batch.setdefault(batch_id, []).append(job)
            for raw_group in groups:
                if not isinstance(raw_group, Mapping):
                    continue
                node_key = str(raw_group.get("integration_node_key") or "").strip()
                members = tuple(
                    str(value or "").strip()
                    for value in raw_group.get("member_node_keys") or ()
                    if str(value or "").strip()
                )
                if not node_key:
                    continue
                certification_successors = sorted(
                    key
                    for key, node in compiled_nodes.items()
                    if str(node.get("node_type") or node.get("kind") or "")
                    == "certification"
                    and node_key
                    in {str(value or "") for value in node.get("depends_on") or ()}
                )
                certification_node_key = (
                    certification_successors[0]
                    if len(certification_successors) == 1
                    else ""
                )
                graph_blocker = (
                    "integration node must have exactly one direct certification "
                    "controller successor"
                    if len(certification_successors) != 1
                    else ""
                )
                if certification_node_key:
                    certification_spec = compiled_nodes[certification_node_key]
                    integration_dependencies = sorted(
                        str(value or "")
                        for value in certification_spec.get("depends_on") or ()
                        if str(
                            _public(compiled_nodes.get(str(value or ""))).get(
                                "node_type"
                            )
                            or _public(compiled_nodes.get(str(value or ""))).get("kind")
                            or ""
                        )
                        == "integration"
                    )
                    if integration_dependencies != [node_key]:
                        graph_blocker = (
                            "automatic certification requires one exact integration "
                            "batch predecessor"
                        )
                item_key = "%s:%d:%d:%s" % (
                    package_id,
                    plan_version,
                    epoch,
                    node_key,
                )
                matching = [
                    batch
                    for batch in batches
                    if str(
                        _public(batch.get("metadata")).get("integration_node_key") or ""
                    )
                    == node_key
                    and str(batch.get("state") or "") in _ACTIONABLE_BATCH_STATES
                ]
                blocker = ""
                if len(matching) > 1:
                    blocker = (
                        "multiple actionable batches exist for one integration group"
                    )
                    batch: Dict[str, Any] = {}
                else:
                    batch = matching[0] if matching else {}
                controller_node = _public(nodes.get(node_key))
                controller_task_id = str(controller_node.get("task_id") or "").strip()
                certification_node = _public(nodes.get(certification_node_key))
                certification_task_id = str(
                    certification_node.get("task_id") or ""
                ).strip()
                if not batch:
                    missing = [
                        member
                        for member in members
                        if str(_public(nodes.get(member)).get("node_state") or "")
                        != "candidate_accepted"
                    ]
                    if missing and not blocker:
                        continue
                    if not members and not blocker:
                        blocker = "integration group has no compiled members"
                    if not controller_node and not blocker:
                        blocker = "integration group has no controller task link"
                    elif (
                        str(controller_node.get("node_state") or "") != "ready"
                        and not blocker
                    ):
                        # A planned controller station is not a worker backlog
                        # item.  Candidate acceptance will expose it by moving
                        # only the package link to ready.
                        continue
                    elif not blocker:
                        task_metadata = _public(controller_node.get("metadata"))
                        projection = _public(task_metadata.get("work_package"))
                        if (
                            str(controller_node.get("task_state") or "") != "waiting"
                            or controller_node.get("owner_agent_id") is not None
                            or controller_node.get("lease_id") is not None
                            or task_metadata.get("no_dispatch") is not True
                            or str(projection.get("node_type") or "") != "integration"
                        ):
                            blocker = (
                                "controller-ready integration task lost its waiting "
                                "state, dispatch hold, or controller route"
                            )
                elif (
                    not controller_task_id
                    or str(batch.get("integration_task_id") or "") != controller_task_id
                ) and not blocker:
                    blocker = (
                        "integration batch is not bound to the exact controller task"
                    )
                if graph_blocker and not blocker:
                    blocker = graph_blocker
                batch_id = str(batch.get("id") or "").strip()
                jobs = jobs_by_batch.get(batch_id, []) if batch_id else []
                if len(jobs) > 1:
                    blocker = "multiple certification jobs exist for one exact batch"
                    job: Dict[str, Any] = {}
                else:
                    job = jobs[0] if jobs else {}
                if job and (
                    not certification_task_id
                    or str(job.get("certification_task_id") or "")
                    != certification_task_id
                    or str(job.get("package_id") or "") != package_id
                    or int(job.get("plan_version") or 0) != plan_version
                    or int(job.get("epoch") or 0) != epoch
                ):
                    blocker = (
                        "certification job is not bound to the exact controller task"
                    )
                if (
                    certification_node
                    and str(certification_node.get("node_state") or "") == "ready"
                ):
                    certification_metadata = _public(certification_node.get("metadata"))
                    certification_projection = _public(
                        certification_metadata.get("work_package")
                    )
                    if (
                        str(certification_node.get("task_state") or "") != "waiting"
                        or certification_node.get("owner_agent_id") is not None
                        or certification_node.get("lease_id") is not None
                        or certification_metadata.get("no_dispatch") is not True
                        or str(certification_projection.get("node_type") or "")
                        != "certification"
                    ):
                        blocker = (
                            "controller-ready certification task lost its waiting "
                            "state, dispatch hold, or controller route"
                        )
                batch_metadata = _public(batch.get("metadata"))
                finalization = _public(batch_metadata.get("product_finalization"))
                rejection = _public(batch_metadata.get("product_rejection"))
                product_finalized = bool(
                    batch.get("product_finalized")
                    or finalization.get("status") == "completed"
                )
                if str(batch.get("state") or "") == "published" and product_finalized:
                    continue
                rejection_finalized = bool(
                    batch.get("rejection_finalized")
                    or rejection.get("status") == "completed"
                )
                if str(batch.get("state") or "") == "rejected" and rejection_finalized:
                    continue
                snapshots.append(
                    PipelineSnapshot(
                        key=item_key,
                        package_id=package_id,
                        plan_version=plan_version,
                        epoch=epoch,
                        integration_node_key=node_key,
                        integration_task_id=controller_task_id,
                        integration_node_state=str(
                            controller_node.get("node_state") or ""
                        ),
                        certification_node_key=certification_node_key,
                        certification_task_id=certification_task_id,
                        certification_node_state=str(
                            certification_node.get("node_state") or ""
                        ),
                        batch_id=batch_id,
                        batch_state=str(batch.get("state") or ""),
                        certification_job_id=str(job.get("id") or ""),
                        certification_job_state=str(job.get("state") or ""),
                        certification_id=str(job.get("certification_id") or ""),
                        product_finalized=product_finalized,
                        rejection_finalized=rejection_finalized,
                        blocker=blocker,
                    )
                )

            if self._paged_catalog:
                snapshots.append(
                    self._catalog_marker(
                        package_id,
                        plan_version,
                        epoch,
                        "catalog traversal marker; no controller station action",
                    )
                )

        snapshots.sort(key=lambda item: item.key)
        if cursor:
            snapshots = [item for item in snapshots if item.key > cursor] + [
                item for item in snapshots if item.key <= cursor
            ]
        return tuple(snapshots[:limit_value])

    def _package_catalog(self, *, cursor: str, limit: int) -> Sequence[Any]:
        if not self._paged_catalog:
            return self._list_packages(state="active", limit=self._catalog_limit)
        parts = cursor.rsplit(":", 3) if cursor else []
        cursor_package = (
            parts[0]
            if len(parts) == 4 and parts[1].isdigit() and parts[2].isdigit()
            else ""
        )
        page_limit = max(1, min(self._catalog_limit, int(limit) + 1))
        primary = self._list_packages(
            state="active",
            limit=page_limit,
            after_id=cursor_package or None,
            order_by_id=True,
        )
        # Include one bounded wrap page.  Catalog markers advance the public
        # cursor even for inactive stations, so repeated runs eventually visit
        # every active package instead of re-reading a fixed first-N window.
        wrapped = (
            self._list_packages(
                state="active",
                limit=page_limit,
                after_id=None,
                order_by_id=True,
            )
            if cursor_package
            else ()
        )
        by_id: Dict[str, Any] = {}
        for raw in tuple(primary) + tuple(wrapped):
            package_id = str(_public(raw).get("id") or "").strip()
            if package_id:
                by_id.setdefault(package_id, raw)
        return tuple(by_id[key] for key in sorted(by_id))

    @staticmethod
    def _catalog_marker(
        package_id: str,
        plan_version: int,
        epoch: int,
        blocker: str,
    ) -> PipelineSnapshot:
        return PipelineSnapshot(
            key="%s:%d:%d:~catalog"
            % (
                package_id,
                int(plan_version),
                int(epoch),
            ),
            package_id=package_id,
            plan_version=int(plan_version),
            epoch=int(epoch),
            integration_node_key="~catalog",
            blocker=blocker,
        )


__all__ = [
    "CertificationBundleProvider",
    "CertificationStation",
    "control_plane_pipeline_observer",
    "IntegrationStation",
    "LandingStation",
    "PipelineInventory",
    "PipelineOutcome",
    "PipelineReleaseGate",
    "PipelineReleaseGateResolver",
    "PipelineRunReport",
    "PipelineSnapshot",
    "ProductFinalizationStation",
    "ProductRejectionStation",
    "ServicePipelineInventory",
    "WORK_PACKAGE_PIPELINE_OUTCOME_SCHEMA",
    "WORK_PACKAGE_PIPELINE_RUN_SCHEMA",
    "WORK_PACKAGE_PIPELINE_SCHEMA",
    "WorkPackagePipelineConfig",
    "WorkPackagePipelineController",
]
