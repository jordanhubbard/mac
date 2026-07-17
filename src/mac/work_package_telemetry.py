"""Durable measurement records for managed versus legacy task execution.

The work-package state machines remain the execution authority.  This module
adds a deliberately append-only measurement plane around them:

* one immutable cohort assignment captures eligibility, treatment route, and
  rollout revision before outcomes are known;
* station-attempt rows capture queue/execution timing and terminal failure
  classification without requiring prunable logs; and
* finalization outcome links attach later reverts or incidents to the exact
  product receipt that introduced the change.

Historical rows are backfilled by the schema only where treatment is durable:
an exact publication finalization proves synchronized treatment, package
linkage alone is an unknown managed mode, and unlinked work was legacy.
Historical experimental eligibility is always ``unknown`` because no
prospective randomization record exists. A surviving control-plane fast-lane
projection is retained separately as exact atomic-shape evidence rather than
being promoted into the later experiment's eligibility contract.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from mac.models import JsonDict, ValidationError, json_dumps, json_loads, new_id, utcnow
from mac.store import Store


COHORT_SCHEMA = "mac.execution_cohort_assignment.v1"
STATION_ATTEMPT_SCHEMA = "mac.work_package.station_attempt.v1"
FINALIZATION_OUTCOME_SCHEMA = "mac.work_package.finalization_outcome.v1"
CONTROLLER_OUTCOME_SCHEMA = "mac.work_package.controller_outcome.v1"
COMPARABLE_OUTCOME_SCHEMA = "mac.execution_cohort.comparable_atomic_outcome.v2"
COHORT_ASSIGNMENT_ALGORITHM = "hmac_sha256_bucket_v1"

ELIGIBILITY_VALUES = frozenset({"eligible", "ineligible", "unknown"})
TREATMENT_ROUTES = frozenset(
    {"legacy_async", "managed_synchronized", "unknown_managed_mode"}
)
STATIONS = frozenset(
    {
        "controller",
        "admission",
        "integration",
        "certification",
        "landing",
        "finalization",
    }
)
TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "busy", "held", "stale", "rejected", "skipped"}
)
FINALIZATION_OUTCOME_TYPES = frozenset({"revert", "incident"})

_PIPELINE_STATIONS = {
    "inventory": "controller",
    "complete": "controller",
    "release_gate": "admission",
    "controller_provenance": "integration",
    "integration_batch": "integration",
    "integration_assembly": "integration",
    "certification_prepare": "certification",
    "certification_run": "certification",
    "certification_acceptance": "certification",
    "certification_rejection": "certification",
    "certification": "certification",
    "landing": "landing",
    "product_finalization": "finalization",
}


def _required(value: Any, label: str, maximum: int = 256) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValidationError("%s is required" % label)
    if len(result) > maximum:
        raise ValidationError("%s may contain at most %d characters" % (label, maximum))
    return result


def _optional(value: Any, maximum: int = 256) -> str:
    result = str(value or "").strip()
    if len(result) > maximum:
        raise ValidationError(
            "telemetry identity may contain at most %d characters" % maximum
        )
    return result


def _json_object(value: Optional[Mapping[str, Any]]) -> JsonDict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError("telemetry detail must be an object")
    result = dict(value)
    try:
        json_dumps(result)
    except (TypeError, ValueError) as exc:
        raise ValidationError("telemetry detail must be JSON serializable") from exc
    return result


def _parse_timestamp(value: str, label: str) -> datetime:
    exact = _required(value, label, maximum=80)
    try:
        parsed = datetime.fromisoformat(exact.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("%s must be an ISO-8601 timestamp" % label) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_timestamp(value: str, label: str) -> str:
    """Return one lexically sortable UTC representation with fixed precision."""

    return (
        _parse_timestamp(value, label)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _duration_ms(started_at: str, completed_at: str) -> int:
    started = _parse_timestamp(started_at, "station started_at")
    completed = _parse_timestamp(completed_at, "station completed_at")
    if completed < started:
        raise ValidationError("station completed_at may not precede started_at")
    return max(0, int((completed - started).total_seconds() * 1000))


def deterministic_cohort_assignment(
    *,
    key: bytes,
    unit_id: str,
    rollout_revision: int,
    treatment_percentage: int,
) -> JsonDict:
    """Assign an atomic request exogenously without persisting key material.

    The task identity exists before routing and is the randomization unit.  A
    shared key makes the result stable across hub replicas while preventing an
    operator from choosing a desired route by inspecting an unkeyed hash.
    Only the bucket/fingerprint and versioned configuration are returned.
    """

    if not isinstance(key, bytes) or len(key) < 32:
        raise ValidationError("cohort assignment key must contain at least 32 bytes")
    identity = _required(unit_id, "cohort assignment unit_id")
    revision = int(rollout_revision)
    percentage = int(treatment_percentage)
    if revision < 1:
        raise ValidationError("cohort rollout revision must be positive")
    if percentage < 0 or percentage > 100:
        raise ValidationError("cohort treatment percentage must be between 0 and 100")
    message = (
        "mac.execution_cohort_assignment.v1\x00%d\x00%s" % (revision, identity)
    ).encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()
    bucket = int(digest[:16], 16) % 10_000
    threshold = percentage * 100
    route = "managed_synchronized" if bucket < threshold else "legacy_async"
    return {
        "schema": "mac.execution_cohort.randomization.v1",
        "algorithm": COHORT_ASSIGNMENT_ALGORITHM,
        "randomization_unit": "task_id",
        "rollout_revision": revision,
        "treatment_percentage": percentage,
        "bucket_basis_points": bucket,
        "threshold_basis_points": threshold,
        "allocation_fingerprint": digest[:16],
        "treatment_route": route,
    }


class WorkPackageTelemetryService:
    """Write and export immutable execution-comparison measurements."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def assert_primary_cohort_configuration(
        self,
        *,
        rollout_revision: int,
        treatment_percentage: int,
        assignment_key_fingerprint: str,
    ) -> None:
        """Fail closed if replicas disagree about one experiment revision."""

        revision = int(rollout_revision)
        percentage = int(treatment_percentage)
        fingerprint = _required(
            assignment_key_fingerprint,
            "cohort assignment key fingerprint",
            80,
        )
        if not fingerprint.startswith("sha256:") or len(fingerprint) != 71:
            raise ValidationError("cohort assignment key fingerprint is malformed")
        now = _canonical_timestamp(utcnow(), "cohort configuration created_at")
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO execution_cohort_configurations ("
                "rollout_revision, algorithm, treatment_percentage, "
                "assignment_key_fingerprint, created_at"
                ") VALUES (?, ?, ?, ?, ?) ON CONFLICT(rollout_revision) DO NOTHING",
                (
                    revision,
                    COHORT_ASSIGNMENT_ALGORITHM,
                    percentage,
                    fingerprint,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM execution_cohort_configurations "
                "WHERE rollout_revision = ?",
                (revision,),
            ).fetchone()
        assert row is not None
        if (
            str(row["algorithm"]) != COHORT_ASSIGNMENT_ALGORITHM
            or int(row["treatment_percentage"]) != percentage
            or str(row["assignment_key_fingerprint"]) != fingerprint
        ):
            raise ValidationError(
                "cohort experiment configuration conflicts with immutable revision"
            )

    @staticmethod
    def _source(conn: Optional[Any], store: Store) -> Any:
        return conn if conn is not None else store

    def assign_cohort(
        self,
        *,
        task_id: Optional[str],
        package_id: Optional[str],
        eligibility: str,
        treatment_route: str,
        rollout_revision: int,
        cohort_key: str,
        reason: str,
        actor: str,
        detail: Optional[Mapping[str, Any]] = None,
        assigned_at: Optional[str] = None,
        conn: Optional[Any] = None,
    ) -> JsonDict:
        """Assign one treatment exactly once, before station outcomes exist."""

        if conn is None:
            with self.store.transaction() as transaction:
                return self.assign_cohort(
                    task_id=task_id,
                    package_id=package_id,
                    eligibility=eligibility,
                    treatment_route=treatment_route,
                    rollout_revision=rollout_revision,
                    cohort_key=cohort_key,
                    reason=reason,
                    actor=actor,
                    detail=detail,
                    assigned_at=assigned_at,
                    conn=transaction,
                )

        exact_task_id = _optional(task_id)
        exact_package_id = _optional(package_id)
        if not exact_task_id and not exact_package_id:
            raise ValidationError("cohort assignment requires task_id or package_id")
        eligibility_value = _required(eligibility, "cohort eligibility", 32)
        if eligibility_value not in ELIGIBILITY_VALUES:
            raise ValidationError(
                "unsupported cohort eligibility: %s" % eligibility_value
            )
        route = _required(treatment_route, "cohort treatment route", 64)
        if route not in TREATMENT_ROUTES:
            raise ValidationError("unsupported cohort treatment route: %s" % route)
        revision = int(rollout_revision)
        if revision < 0:
            raise ValidationError("cohort rollout revision may not be negative")
        key = _required(cohort_key, "cohort key", 256)
        reason_value = _required(reason, "cohort assignment reason", 1000)
        actor_value = _required(actor, "cohort assignment actor", 256)
        detail_value = _json_object(detail)
        timestamp = _canonical_timestamp(assigned_at or utcnow(), "cohort assigned_at")
        source = self._source(conn, self.store)

        clauses = []
        params: list[Any] = []
        if exact_task_id:
            clauses.append("task_id = ?")
            params.append(exact_task_id)
        if exact_package_id:
            clauses.append("package_id = ?")
            params.append(exact_package_id)
        existing = source.execute(
            "SELECT * FROM execution_cohort_assignments WHERE " + " OR ".join(clauses),
            tuple(params),
        ).fetchone()
        expected = {
            "task_id": exact_task_id or None,
            "package_id": exact_package_id or None,
            "eligibility": eligibility_value,
            "treatment_route": route,
            "rollout_revision": revision,
            "cohort_key": key,
            "reason": reason_value,
            "detail": json_dumps(detail_value),
            "assigned_by": actor_value,
        }
        if existing is not None:
            observed = {name: existing[name] for name in expected}
            observed["rollout_revision"] = int(observed["rollout_revision"])
            observed["detail"] = json_dumps(json_loads(observed["detail"], {}))
            if observed != expected:
                raise ValidationError("execution cohort assignment is immutable")
            return self._assignment(dict(existing))

        assignment_id = new_id("cohort")
        source.execute(
            "INSERT INTO execution_cohort_assignments ("
            "id, task_id, package_id, eligibility, treatment_route, rollout_revision, "
            "cohort_key, reason, detail, assigned_by, assigned_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                assignment_id,
                exact_task_id or None,
                exact_package_id or None,
                eligibility_value,
                route,
                revision,
                key,
                reason_value,
                json_dumps(detail_value),
                actor_value,
                timestamp,
            ),
        )
        row = source.execute(
            "SELECT * FROM execution_cohort_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
        assert row is not None
        return self._assignment(dict(row))

    def record_station_attempt(
        self,
        *,
        package_id: str,
        station: str,
        operation: str,
        attempted: bool,
        terminal_status: str,
        queued_at: str,
        started_at: str,
        completed_at: str,
        actor: str,
        plan_version: Optional[int] = None,
        epoch: Optional[int] = None,
        pipeline_run_id: str = "",
        outcome_index: int = 0,
        batch_id: str = "",
        job_id: str = "",
        reason_code: str = "",
        failure_class: str = "",
        detail: Optional[Mapping[str, Any]] = None,
        conn: Optional[Any] = None,
    ) -> JsonDict:
        """Append one immutable station attempt or held/deferred observation."""

        if conn is None:
            with self.store.transaction() as transaction:
                return self.record_station_attempt(
                    package_id=package_id,
                    station=station,
                    operation=operation,
                    attempted=attempted,
                    terminal_status=terminal_status,
                    queued_at=queued_at,
                    started_at=started_at,
                    completed_at=completed_at,
                    actor=actor,
                    plan_version=plan_version,
                    epoch=epoch,
                    pipeline_run_id=pipeline_run_id,
                    outcome_index=outcome_index,
                    batch_id=batch_id,
                    job_id=job_id,
                    reason_code=reason_code,
                    failure_class=failure_class,
                    detail=detail,
                    conn=transaction,
                )

        package = _required(package_id, "station package_id")
        station_value = _required(station, "station", 64)
        if station_value not in STATIONS:
            raise ValidationError(
                "unsupported work-package station: %s" % station_value
            )
        operation_value = _required(operation, "station operation", 128)
        status = _required(terminal_status, "station terminal status", 32)
        if status not in TERMINAL_STATUSES:
            raise ValidationError("unsupported station terminal status: %s" % status)
        actor_value = _required(actor, "station actor")
        run_id = _optional(pipeline_run_id)
        index = int(outcome_index)
        if index < 0:
            raise ValidationError("station outcome index may not be negative")
        queue_value = _canonical_timestamp(queued_at, "station queued_at")
        started_value = _canonical_timestamp(started_at, "station started_at")
        completed_value = _canonical_timestamp(completed_at, "station completed_at")
        detail_value = _json_object(detail)
        clock_clamps: list[JsonDict] = []
        queue_started = _parse_timestamp(queue_value, "station queued_at")
        execution_started = _parse_timestamp(started_value, "station started_at")
        if execution_started < queue_started:
            # A reconstructed readiness timestamp can lag a controller sample
            # under replica clock skew. Clamp rather than manufacture a
            # negative queue duration.
            clock_clamps.append(
                {
                    "applied": True,
                    "field": "queued_at",
                    "observed": queue_value,
                    "clamped_to": started_value,
                    "reason": "queued_at_after_started_at",
                }
            )
            queue_value = started_value
        execution_completed = _parse_timestamp(completed_value, "station completed_at")
        if execution_completed < execution_started:
            clock_clamps.append(
                {
                    "applied": True,
                    "field": "completed_at",
                    "observed": completed_value,
                    "clamped_to": started_value,
                    "reason": "completed_at_before_started_at",
                }
            )
            completed_value = started_value
        if clock_clamps:
            detail_value["clock_clamps"] = clock_clamps
        queue_ms = _duration_ms(queue_value, started_value)
        execution_ms = _duration_ms(started_value, completed_value)
        source = self._source(conn, self.store)

        if run_id:
            existing = source.execute(
                "SELECT * FROM work_package_station_attempts "
                "WHERE pipeline_run_id = ? AND outcome_index = ?",
                (run_id, index),
            ).fetchone()
            if existing is not None:
                expected_identity = (
                    package,
                    station_value,
                    operation_value,
                    1 if attempted else 0,
                    _optional(batch_id),
                    _optional(job_id),
                    queue_value,
                    started_value,
                    completed_value,
                    status,
                    _optional(reason_code),
                    _optional(failure_class),
                    json_dumps(detail_value),
                )
                observed_identity = (
                    str(existing["package_id"]),
                    str(existing["station"]),
                    str(existing["operation"]),
                    int(existing["attempted"]),
                    str(existing["batch_id"]),
                    str(existing["job_id"]),
                    str(existing["queued_at"]),
                    str(existing["started_at"]),
                    str(existing["completed_at"]),
                    str(existing["terminal_status"]),
                    str(existing["reason_code"]),
                    str(existing["failure_class"]),
                    json_dumps(json_loads(existing["detail"], {})),
                )
                if observed_identity != expected_identity:
                    raise ValidationError(
                        "pipeline outcome identity is already bound to a different attempt"
                    )
                return self._attempt(dict(existing))

        package_row = source.execute(
            "SELECT current_plan_version, current_epoch FROM work_packages WHERE id = ?",
            (package,),
        ).fetchone()
        if package_row is None:
            raise ValidationError("station work package was not found")
        version = int(plan_version or package_row["current_plan_version"] or 0)
        epoch_value = int(epoch or package_row["current_epoch"] or 0)
        if version < 1 or epoch_value < 1:
            raise ValidationError(
                "station attempt requires a positive plan version and epoch"
            )

        assignment = source.execute(
            "SELECT id FROM execution_cohort_assignments WHERE package_id = ? "
            "ORDER BY assigned_at, id LIMIT 1",
            (package,),
        ).fetchone()
        if assignment is None:
            raise ValidationError(
                "station attempt requires an immutable cohort assignment"
            )

        # Serialize attempt ordinals per package on PostgreSQL; SQLite's write
        # transaction already serializes the same calculation.
        source.execute(
            "UPDATE work_packages SET updated_at = updated_at WHERE id = ?", (package,)
        )
        ordinal_row = source.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next "
            "FROM work_package_station_attempts WHERE package_id = ? AND station = ?",
            (package, station_value),
        ).fetchone()
        attempt_number = int(ordinal_row["next"])
        attempt_id = new_id("station_attempt")
        source.execute(
            "INSERT INTO work_package_station_attempts ("
            "id, assignment_id, package_id, plan_version, epoch, station, operation, "
            "attempt_number, attempted, pipeline_run_id, outcome_index, batch_id, job_id, "
            "queued_at, started_at, completed_at, queue_duration_ms, execution_duration_ms, "
            "terminal_status, reason_code, failure_class, actor, detail, recorded_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                assignment["id"],
                package,
                version,
                epoch_value,
                station_value,
                operation_value,
                attempt_number,
                1 if attempted else 0,
                run_id,
                index,
                _optional(batch_id),
                _optional(job_id),
                queue_value,
                started_value,
                completed_value,
                queue_ms,
                execution_ms,
                status,
                _optional(reason_code),
                _optional(failure_class),
                actor_value,
                json_dumps(detail_value),
                _canonical_timestamp(utcnow(), "station recorded_at"),
            ),
        )
        row = source.execute(
            "SELECT * FROM work_package_station_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        assert row is not None
        return self._attempt(dict(row))

    def record_pipeline_report(self, report: Mapping[str, Any]) -> list[JsonDict]:
        """Project one bounded controller report into append-only attempts."""

        if not isinstance(report, Mapping):
            raise ValidationError("pipeline report must be an object")
        run_id = _required(report.get("run_id"), "pipeline run_id")
        actor = _optional(report.get("actor")) or "work-package-pipeline"
        outcomes = report.get("outcomes") or []
        if not isinstance(outcomes, Sequence) or isinstance(outcomes, (str, bytes)):
            raise ValidationError("pipeline outcomes must be a list")
        report_started = _canonical_timestamp(
            str(report.get("started_at") or utcnow()), "pipeline report started_at"
        )
        report_completed = _canonical_timestamp(
            str(report.get("completed_at") or report_started),
            "pipeline report completed_at",
        )
        report_status = str(report.get("status") or "unknown").strip() or "unknown"

        # The raw controller ledger is deliberately committed first.  A stale
        # package link or normalization defect must not erase the very outcome
        # that is needed to diagnose why a station projection failed.
        with self.store.transaction() as conn:
            self._record_controller_outcome(
                conn,
                run_id=run_id,
                outcome_index=-1,
                operation="controller_run",
                package_id="",
                plan_version=0,
                epoch=0,
                attempted=False,
                started_at=report_started,
                completed_at=report_completed,
                status=report_status,
                code="controller_run_%s" % report_status,
                detail={
                    "trigger": report.get("trigger"),
                    "scanned_count": int(report.get("scanned_count") or 0),
                    "action_count": int(report.get("action_count") or 0),
                    "outcome_count": len(outcomes),
                },
            )
            for index, raw in enumerate(outcomes):
                if isinstance(raw, Mapping):
                    operation = (
                        str(raw.get("station") or "unmapped").strip() or "unmapped"
                    )
                    raw_detail = _json_object(
                        raw.get("detail")
                        if isinstance(raw.get("detail"), Mapping)
                        else {}
                    )
                    self._record_controller_outcome(
                        conn,
                        run_id=run_id,
                        outcome_index=index,
                        operation=operation,
                        package_id=str(raw.get("package_id") or ""),
                        plan_version=int(raw.get("plan_version") or 0),
                        epoch=int(raw.get("epoch") or 0),
                        attempted=bool(raw.get("attempted")),
                        started_at=str(raw.get("started_at") or report_started),
                        completed_at=str(raw.get("completed_at") or report_completed),
                        status=str(raw.get("status") or "unknown"),
                        code=str(raw.get("code") or ""),
                        batch_id=str(raw.get("batch_id") or ""),
                        job_id=str(raw.get("job_id") or ""),
                        detail={
                            **raw_detail,
                            "normalization_station": _PIPELINE_STATIONS.get(
                                operation, "controller"
                            ),
                            "operation_mapped": operation in _PIPELINE_STATIONS,
                        },
                    )
                else:
                    self._record_controller_outcome(
                        conn,
                        run_id=run_id,
                        outcome_index=index,
                        operation="unmapped_payload",
                        package_id="",
                        plan_version=0,
                        epoch=0,
                        attempted=False,
                        started_at=report_started,
                        completed_at=report_completed,
                        status="failed",
                        code="invalid_pipeline_outcome_payload",
                        detail={"payload_type": type(raw).__name__},
                    )

        recorded: list[JsonDict] = []
        with self.store.transaction() as conn:
            for index, raw in enumerate(outcomes):
                if not isinstance(raw, Mapping):
                    continue
                operation = str(raw.get("station") or "").strip()
                station = _PIPELINE_STATIONS.get(operation, "controller")
                package_id = str(raw.get("package_id") or "").strip()
                if not package_id:
                    continue
                package_assignment = conn.execute(
                    "SELECT 1 FROM work_packages AS package "
                    "JOIN execution_cohort_assignments AS assignment "
                    "ON assignment.package_id = package.id WHERE package.id = ?",
                    (package_id,),
                ).fetchone()
                if package_assignment is None:
                    continue
                started_at = str(raw.get("started_at") or report_started)
                completed_at = str(raw.get("completed_at") or report_completed)
                detail = _json_object(
                    raw.get("detail") if isinstance(raw.get("detail"), Mapping) else {}
                )
                code = str(raw.get("code") or "")
                terminal_status, failure_class = self._classify_pipeline_outcome(
                    operation=operation,
                    status=str(raw.get("status") or ""),
                    code=code,
                    detail=detail,
                )
                queued_at = self._queue_started_at(
                    conn,
                    package_id=package_id,
                    station=station,
                    batch_id=str(raw.get("batch_id") or ""),
                    job_id=str(raw.get("job_id") or ""),
                    fallback=started_at,
                )
                recorded.append(
                    self.record_station_attempt(
                        package_id=package_id,
                        station=station,
                        operation=operation,
                        attempted=bool(raw.get("attempted")),
                        terminal_status=terminal_status,
                        queued_at=queued_at,
                        started_at=started_at,
                        completed_at=completed_at,
                        actor=actor,
                        plan_version=(
                            int(raw["plan_version"])
                            if raw.get("plan_version") is not None
                            else None
                        ),
                        epoch=(
                            int(raw["epoch"]) if raw.get("epoch") is not None else None
                        ),
                        pipeline_run_id=run_id,
                        outcome_index=index,
                        batch_id=str(raw.get("batch_id") or ""),
                        job_id=str(raw.get("job_id") or ""),
                        reason_code=code,
                        failure_class=(
                            failure_class
                            if operation in _PIPELINE_STATIONS
                            else "unmapped_controller_operation"
                        ),
                        detail={
                            **detail,
                            "operation_mapped": operation in _PIPELINE_STATIONS,
                        },
                        conn=conn,
                    )
                )
        return recorded

    def _record_controller_outcome(
        self,
        conn: Any,
        *,
        run_id: str,
        outcome_index: int,
        operation: str,
        package_id: str,
        plan_version: int,
        epoch: int,
        attempted: bool,
        started_at: str,
        completed_at: str,
        status: str,
        code: str,
        batch_id: str = "",
        job_id: str = "",
        detail: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        operation_value = _required(operation, "controller operation", 128)
        started_value = _canonical_timestamp(started_at, "controller started_at")
        completed_value = _canonical_timestamp(completed_at, "controller completed_at")
        detail_value = _json_object(detail)
        if _parse_timestamp(
            completed_value, "controller completed_at"
        ) < _parse_timestamp(started_value, "controller started_at"):
            detail_value["clock_clamps"] = [
                {
                    "applied": True,
                    "field": "completed_at",
                    "observed": completed_value,
                    "clamped_to": started_value,
                    "reason": "completed_at_before_started_at",
                }
            ]
            completed_value = started_value
        status_value = _required(status, "controller outcome status", 64)
        terminal_status, failure_class = self._classify_pipeline_outcome(
            operation=operation_value,
            status=status_value,
            code=str(code or ""),
            detail=detail_value,
        )
        if (
            operation_value not in _PIPELINE_STATIONS
            and operation_value != "controller_run"
        ):
            failure_class = "unmapped_controller_operation"
        existing = conn.execute(
            "SELECT * FROM work_package_controller_outcomes "
            "WHERE pipeline_run_id = ? AND outcome_index = ?",
            (run_id, int(outcome_index)),
        ).fetchone()
        identity = (
            operation_value,
            str(package_id or "").strip(),
            int(plan_version),
            int(epoch),
            1 if attempted else 0,
            _optional(batch_id),
            _optional(job_id),
            started_value,
            completed_value,
            status_value,
            terminal_status,
            str(code or "").strip(),
            failure_class,
            json_dumps(detail_value),
        )
        if existing is not None:
            observed = tuple(
                existing[name]
                for name in (
                    "operation",
                    "package_id",
                    "plan_version",
                    "epoch",
                    "attempted",
                    "batch_id",
                    "job_id",
                    "started_at",
                    "completed_at",
                    "status",
                    "terminal_status",
                    "reason_code",
                    "failure_class",
                    "detail",
                )
            )
            observed = (
                str(observed[0]),
                str(observed[1]),
                int(observed[2]),
                int(observed[3]),
                int(observed[4]),
                str(observed[5]),
                str(observed[6]),
                str(observed[7]),
                str(observed[8]),
                str(observed[9]),
                str(observed[10]),
                str(observed[11]),
                str(observed[12]),
                json_dumps(json_loads(observed[13], {})),
            )
            if observed != identity:
                raise ValidationError(
                    "pipeline outcome identity is already bound to different raw evidence"
                )
            return self._controller_outcome(dict(existing))
        outcome_id = new_id("controller_outcome")
        conn.execute(
            "INSERT INTO work_package_controller_outcomes ("
            "id, pipeline_run_id, outcome_index, package_id, plan_version, epoch, "
            "operation, attempted, batch_id, job_id, started_at, completed_at, "
            "execution_duration_ms, status, terminal_status, reason_code, "
            "failure_class, detail, recorded_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                outcome_id,
                run_id,
                int(outcome_index),
                str(package_id or "").strip(),
                int(plan_version),
                int(epoch),
                operation_value,
                1 if attempted else 0,
                _optional(batch_id),
                _optional(job_id),
                started_value,
                completed_value,
                _duration_ms(started_value, completed_value),
                status_value,
                terminal_status,
                str(code or "").strip(),
                failure_class,
                json_dumps(detail_value),
                _canonical_timestamp(utcnow(), "controller outcome recorded_at"),
            ),
        )
        row = conn.execute(
            "SELECT * FROM work_package_controller_outcomes WHERE id = ?",
            (outcome_id,),
        ).fetchone()
        assert row is not None
        return self._controller_outcome(dict(row))

    def record_finalization_outcome(
        self,
        finalization_id: str,
        *,
        outcome_type: str,
        external_id: str,
        observed_at: str,
        actor: str,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        """Link a later revert or incident to an exact finalization receipt."""

        finalization = _required(finalization_id, "finalization id")
        kind = _required(outcome_type, "finalization outcome type", 32)
        if kind not in FINALIZATION_OUTCOME_TYPES:
            raise ValidationError(
                "finalization outcome type must be revert or incident"
            )
        external = _required(external_id, "finalization outcome external_id", 512)
        actor_value = _required(actor, "finalization outcome actor")
        timestamp = _canonical_timestamp(
            observed_at, "finalization outcome observed_at"
        )
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT package_id FROM work_package_publication_finalizations WHERE id = ?",
                (finalization,),
            ).fetchone()
            if row is None:
                raise ValidationError("work-package finalization was not found")
            existing = conn.execute(
                "SELECT * FROM work_package_finalization_outcomes "
                "WHERE finalization_id = ? AND outcome_type = ? AND external_id = ?",
                (finalization, kind, external),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["observed_at"]) != timestamp
                    or str(existing["actor"]) != actor_value
                    or json_loads(existing["detail"], {}) != _json_object(detail)
                ):
                    raise ValidationError(
                        "finalization outcome identity is already bound to different evidence"
                    )
                return self._outcome_link(dict(existing))
            outcome_id = new_id("finalization_outcome")
            conn.execute(
                "INSERT INTO work_package_finalization_outcomes ("
                "id, finalization_id, package_id, outcome_type, external_id, observed_at, "
                "actor, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    outcome_id,
                    finalization,
                    row["package_id"],
                    kind,
                    external,
                    timestamp,
                    actor_value,
                    json_dumps(_json_object(detail)),
                    _canonical_timestamp(utcnow(), "finalization outcome created_at"),
                ),
            )
            inserted = conn.execute(
                "SELECT * FROM work_package_finalization_outcomes WHERE id = ?",
                (outcome_id,),
            ).fetchone()
            assert inserted is not None
            return self._outcome_link(dict(inserted))

    def export(
        self,
        *,
        package_id: Optional[str] = None,
        treatment_route: Optional[str] = None,
        eligibility: Optional[str] = None,
        station: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 1000,
    ) -> JsonDict:
        """Return a stable, bounded evaluation export with explicit limitations."""

        route = _optional(treatment_route, 64)
        if route and route not in TREATMENT_ROUTES:
            raise ValidationError("unsupported cohort treatment route: %s" % route)
        eligibility_value = _optional(eligibility, 32)
        if eligibility_value and eligibility_value not in ELIGIBILITY_VALUES:
            raise ValidationError(
                "unsupported cohort eligibility: %s" % eligibility_value
            )
        station_value = _optional(station, 64)
        if station_value and station_value not in STATIONS:
            raise ValidationError(
                "unsupported work-package station: %s" % station_value
            )
        since_value = (
            _canonical_timestamp(since, "telemetry export since") if since else None
        )
        limit_value = max(1, min(int(limit), 10_000))

        assignment_clauses: list[str] = []
        assignment_params: list[Any] = []
        if package_id:
            assignment_clauses.append("package_id = ?")
            assignment_params.append(package_id)
        if route:
            assignment_clauses.append("treatment_route = ?")
            assignment_params.append(route)
        if eligibility_value:
            assignment_clauses.append("eligibility = ?")
            assignment_params.append(eligibility_value)
        if since_value:
            assignment_clauses.append("assigned_at >= ?")
            assignment_params.append(since_value)
        assignment_sql = "SELECT * FROM execution_cohort_assignments"
        if assignment_clauses:
            assignment_sql += " WHERE " + " AND ".join(assignment_clauses)
        assignment_sql += " ORDER BY assigned_at, id LIMIT ?"
        assignment_params.append(limit_value)
        assignments = [
            self._assignment(dict(row))
            for row in self.store.query_all(assignment_sql, tuple(assignment_params))
        ]

        attempt_clauses: list[str] = []
        attempt_params: list[Any] = []
        if package_id:
            attempt_clauses.append("attempt.package_id = ?")
            attempt_params.append(package_id)
        if route:
            attempt_clauses.append("assignment.treatment_route = ?")
            attempt_params.append(route)
        if eligibility_value:
            attempt_clauses.append("assignment.eligibility = ?")
            attempt_params.append(eligibility_value)
        if station_value:
            attempt_clauses.append("attempt.station = ?")
            attempt_params.append(station_value)
        if since_value:
            attempt_clauses.append("attempt.completed_at >= ?")
            attempt_params.append(since_value)
        attempt_sql = (
            "SELECT attempt.* FROM work_package_station_attempts AS attempt "
            "JOIN execution_cohort_assignments AS assignment "
            "ON assignment.id = attempt.assignment_id"
        )
        if attempt_clauses:
            attempt_sql += " WHERE " + " AND ".join(attempt_clauses)
        attempt_sql += " ORDER BY attempt.completed_at, attempt.id LIMIT ?"
        attempt_params.append(limit_value)
        attempts = [
            self._attempt(dict(row))
            for row in self.store.query_all(attempt_sql, tuple(attempt_params))
        ]

        controller_clauses: list[str] = []
        controller_params: list[Any] = []
        controller_join = ""
        if route or eligibility_value:
            controller_join = (
                " JOIN execution_cohort_assignments AS assignment "
                "ON assignment.package_id = controller.package_id"
            )
        if package_id:
            controller_clauses.append("controller.package_id = ?")
            controller_params.append(package_id)
        if route:
            controller_clauses.append("assignment.treatment_route = ?")
            controller_params.append(route)
        if eligibility_value:
            controller_clauses.append("assignment.eligibility = ?")
            controller_params.append(eligibility_value)
        if station_value:
            operations = sorted(
                operation
                for operation, normalized in _PIPELINE_STATIONS.items()
                if normalized == station_value
            )
            if station_value == "controller":
                controller_clauses.append(
                    "(controller.operation IN (%s) OR controller.operation = ? OR "
                    "controller.operation NOT IN (%s))"
                    % (
                        ",".join("?" for _ in operations),
                        ",".join("?" for _ in _PIPELINE_STATIONS),
                    )
                )
                controller_params.extend(operations)
                controller_params.append("controller_run")
                controller_params.extend(sorted(_PIPELINE_STATIONS))
            elif operations:
                controller_clauses.append(
                    "controller.operation IN (%s)" % ",".join("?" for _ in operations)
                )
                controller_params.extend(operations)
        if since_value:
            controller_clauses.append("controller.completed_at >= ?")
            controller_params.append(since_value)
        controller_sql = (
            "SELECT controller.* FROM work_package_controller_outcomes AS controller"
            + controller_join
        )
        if controller_clauses:
            controller_sql += " WHERE " + " AND ".join(controller_clauses)
        controller_sql += " ORDER BY controller.completed_at, controller.id LIMIT ?"
        controller_params.append(limit_value)
        controller_outcomes = [
            self._controller_outcome(dict(row))
            for row in self.store.query_all(controller_sql, tuple(controller_params))
        ]

        outcome_clauses: list[str] = []
        outcome_params: list[Any] = []
        outcome_join = ""
        if route or eligibility_value:
            outcome_join = (
                " JOIN execution_cohort_assignments AS assignment "
                "ON assignment.package_id = outcome.package_id"
            )
        if package_id:
            outcome_clauses.append("outcome.package_id = ?")
            outcome_params.append(package_id)
        if route:
            outcome_clauses.append("assignment.treatment_route = ?")
            outcome_params.append(route)
        if eligibility_value:
            outcome_clauses.append("assignment.eligibility = ?")
            outcome_params.append(eligibility_value)
        if since_value:
            outcome_clauses.append("outcome.observed_at >= ?")
            outcome_params.append(since_value)
        outcome_sql = (
            "SELECT outcome.* FROM work_package_finalization_outcomes AS outcome"
            + outcome_join
        )
        if outcome_clauses:
            outcome_sql += " WHERE " + " AND ".join(outcome_clauses)
        outcome_sql += " ORDER BY outcome.observed_at, outcome.id LIMIT ?"
        outcome_params.append(limit_value)
        outcomes = [
            self._outcome_link(dict(row))
            for row in self.store.query_all(outcome_sql, tuple(outcome_params))
        ]
        health_row = self.store.query_one(
            "SELECT * FROM work_package_telemetry_health WHERE singleton_key = ?",
            ("pipeline",),
        )
        configurations = [
            {
                **dict(row),
                "rollout_revision": int(row["rollout_revision"]),
                "treatment_percentage": int(row["treatment_percentage"]),
                "schema": "mac.execution_cohort_configuration.v1",
            }
            for row in self.store.query_all(
                "SELECT * FROM execution_cohort_configurations "
                "ORDER BY rollout_revision LIMIT ?",
                (limit_value,),
            )
        ]
        return {
            "schema": "mac.work_package.telemetry_export.v1",
            "filters": {
                "package_id": package_id,
                "treatment_route": route or None,
                "eligibility": eligibility_value or None,
                "station": station_value or None,
                "since": since_value,
                "limit": limit_value,
            },
            "cohort_configurations": configurations,
            "cohort_assignments": assignments,
            "station_attempts": attempts,
            "controller_outcomes": controller_outcomes,
            "finalization_outcomes": outcomes,
            "comparable_atomic_outcomes": self.comparable_atomic_outcomes(
                package_id=package_id,
                treatment_route=route or None,
                since=since_value,
                limit=limit_value,
            ),
            "measurement_health": self._measurement_health(health_row),
            "limitations": [
                "Historical package linkage alone is unknown_managed_mode; synchronized treatment requires a durable publication finalization receipt.",
                "Historical experimental eligibility is unknown; a surviving managed-fast-lane route projection is retained only as atomic-shape evidence and is not promoted into the prospective experiment.",
                "Station timing is prospective; historical station durations are not reconstructed from mutable timestamps.",
                "The primary comparable projection estimates the intention-to-treat canonical-publication outcome for concurrent atomic auto-policy requests; route-internal task completion is explicitly secondary.",
                "Legacy success requires a publication receipt and Git-main work additionally requires canonical-integration proof; managed success requires an atomic publication-finalization receipt.",
                "Managed package failure/cancellation, final certification rejection, and exhausted candidate rework are terminal failures; recoverable pause and replanning states remain censored.",
                "Randomization is persisted before route-specific materialization; task_materialized=false therefore remains in the primary cohort and must be analyzed with a predeclared observation window rather than dropped as missing data.",
            ],
        }

    def comparable_atomic_outcomes(
        self,
        *,
        package_id: Optional[str] = None,
        treatment_route: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 1000,
    ) -> list[JsonDict]:
        """Project the predeclared canonical-publication ITT comparison set.

        Both arms start at the immutable assignment clock and stop only at a
        semantically equivalent product boundary.  Legacy work succeeds after
        its durable publication record (and, for Git main, canonical-integration
        proof); managed work succeeds after the atomic package publication
        finalization.  Route-internal task completion remains useful diagnostic
        data, but is explicitly secondary because a managed mutation task can
        complete before certification, landing, and product finalization.
        """

        route = _optional(treatment_route, 64)
        if route and route not in {"legacy_async", "managed_synchronized"}:
            return []
        since_value = (
            _canonical_timestamp(since, "comparable outcome since") if since else None
        )
        limit_value = max(1, min(int(limit), 10_000))
        clauses = ["assignment.task_id IS NOT NULL", "assignment.eligibility = ?"]
        params: list[Any] = ["eligible"]
        exact_package_id = _optional(package_id)
        if exact_package_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM work_packages AS selected_package "
                "WHERE selected_package.root_task_id = task.id "
                "AND selected_package.id = ?)"
            )
            params.append(exact_package_id)
        if route:
            clauses.append("assignment.treatment_route = ?")
            params.append(route)
        if since_value:
            clauses.append("assignment.assigned_at >= ?")
            params.append(since_value)
        sql = (
            "SELECT assignment.*, task.id AS materialized_task_id, "
            "task.state AS task_state, "
            "task.attempt_count, task.created_at AS task_created_at, "
            "task.started_at AS task_started_at, task.completed_at AS task_completed_at, "
            "task.updated_at AS task_updated_at, "
            "(SELECT package.id FROM work_packages AS package "
            " WHERE package.root_task_id = task.id ORDER BY package.created_at, package.id "
            " LIMIT 1) AS linked_package_id "
            "FROM execution_cohort_assignments AS assignment "
            "LEFT JOIN tasks AS task ON task.id = assignment.task_id "
            "WHERE "
            + " AND ".join(clauses)
            + " ORDER BY assignment.assigned_at, assignment.id LIMIT ?"
        )
        params.append(limit_value)
        projected: list[JsonDict] = []
        for source in self.store.query_all(sql, tuple(params)):
            detail = json_loads(source["detail"], {})
            randomization = (
                detail.get("randomization") if isinstance(detail, Mapping) else None
            )
            if (
                not isinstance(randomization, Mapping)
                or randomization.get("algorithm") != COHORT_ASSIGNMENT_ALGORITHM
                or detail.get("schema") != "mac.execution_cohort.prospective.v3"
                or detail.get("estimand")
                != "intention_to_treat_canonical_publication_outcome"
                or detail.get("primary_analysis_eligible") is not True
            ):
                continue
            materialized = bool(source["materialized_task_id"])
            created_at = (
                _canonical_timestamp(
                    source["task_created_at"], "comparable task created_at"
                )
                if materialized and source["task_created_at"]
                else None
            )
            started_at = (
                _canonical_timestamp(
                    source["task_started_at"], "comparable task started_at"
                )
                if source["task_started_at"]
                else None
            )
            completed_at = (
                _canonical_timestamp(
                    source["task_completed_at"], "comparable task completed_at"
                )
                if source["task_completed_at"]
                else None
            )
            package_id = str(source["linked_package_id"] or "").strip() or None
            package = None
            if package_id:
                package = self.store.query_one(
                    "SELECT state, metadata, completed_at, updated_at "
                    "FROM work_packages WHERE id = ?",
                    (package_id,),
                )
            finalization = None
            product_outcome_count = 0
            if package_id:
                finalization = self.store.query_one(
                    "SELECT id, finalized_at FROM work_package_publication_finalizations "
                    "WHERE package_id = ? ORDER BY finalized_at DESC, id DESC LIMIT 1",
                    (package_id,),
                )
                if finalization is not None:
                    product_outcome_count = int(
                        self.store.query_one(
                            "SELECT COUNT(*) AS n FROM work_package_finalization_outcomes "
                            "WHERE finalization_id = ?",
                            (str(finalization["id"]),),
                        )["n"]
                    )
            if source["treatment_route"] == "legacy_async":
                endpoint = self._legacy_publication_endpoint(
                    task_id=str(source["task_id"]),
                    task_state=(
                        str(source["task_state"]) if materialized else None
                    ),
                    task_completed_at=completed_at,
                    task_updated_at=(
                        str(source["task_updated_at"])
                        if materialized and source["task_updated_at"]
                        else None
                    ),
                )
            else:
                endpoint = self._managed_publication_endpoint(
                    package_id=package_id,
                    package=package,
                    finalization=finalization,
                )
            terminal_at = endpoint["terminal_at"]
            projected.append(
                {
                    "schema": COMPARABLE_OUTCOME_SCHEMA,
                    "assignment_id": source["id"],
                    "task_id": source["task_id"],
                    "package_id": package_id,
                    "rollout_revision": int(source["rollout_revision"]),
                    "treatment_route": source["treatment_route"],
                    "assigned_at": _canonical_timestamp(
                        source["assigned_at"], "comparable assigned_at"
                    ),
                    "randomization": dict(randomization),
                    "task_materialized": materialized,
                    "canonical_publication_outcome": endpoint["outcome"],
                    "canonical_publication_terminal": bool(
                        endpoint["terminal"]
                    ),
                    "canonical_publication_success": endpoint["success"],
                    "canonical_publication_terminal_at": terminal_at,
                    "assignment_to_canonical_publication_terminal_duration_ms": (
                        _duration_ms(source["assigned_at"], terminal_at)
                        if terminal_at
                        else None
                    ),
                    "canonical_publication_proof": endpoint["proof"],
                    "canonical_publication_failure_class": endpoint[
                        "failure_class"
                    ],
                    "censoring_reason": endpoint["censoring_reason"],
                    "secondary_task_metrics": {
                        "role": "secondary_route_internal",
                        "state": source["task_state"] if materialized else None,
                        "success": (
                            source["task_state"] == "completed"
                            if completed_at
                            else None
                        ),
                        "terminal": completed_at is not None,
                        "attempt_count": (
                            int(source["attempt_count"])
                            if materialized
                            else None
                        ),
                        "created_at": created_at,
                        "started_at": started_at,
                        "completed_at": completed_at,
                        "queue_duration_ms": (
                            _duration_ms(created_at, started_at)
                            if created_at and started_at
                            else None
                        ),
                        "terminal_duration_ms": (
                            _duration_ms(created_at, completed_at)
                            if created_at and completed_at
                            else None
                        ),
                        "assignment_to_terminal_duration_ms": (
                            _duration_ms(source["assigned_at"], completed_at)
                            if completed_at
                            else None
                        ),
                    },
                    "product_finalization_id": (
                        str(finalization["id"])
                        if finalization is not None
                        else None
                    ),
                    "product_finalized_at": (
                        _canonical_timestamp(
                            finalization["finalized_at"],
                            "comparable product finalized_at",
                        )
                        if finalization is not None
                        else None
                    ),
                    "product_outcome_count": product_outcome_count,
                    "product_outcome_role": "secondary_post_publication_quality",
                    "primary_estimand": (
                        "intention_to_treat_canonical_publication_outcome"
                    ),
                }
            )
        return projected

    def _legacy_publication_endpoint(
        self,
        *,
        task_id: str,
        task_state: Optional[str],
        task_completed_at: Optional[str],
        task_updated_at: Optional[str],
    ) -> JsonDict:
        """Resolve the legacy arm at its durable canonical publication edge."""

        publication = self.store.query_one(
            "SELECT id, target, status, created_at FROM publications "
            "WHERE task_id = ? AND status = 'published' "
            "ORDER BY created_at, id LIMIT 1",
            (task_id,),
        )
        proof: Optional[JsonDict] = None
        if publication is not None:
            target = str(publication["target"])
            proof = {
                "type": "legacy_publication_receipt",
                "id": str(publication["id"]),
                "target": target,
            }
            if target in {"git://main", "git://origin/main"}:
                integration = self._canonical_integration_proof(
                    task_id,
                    published_at=str(publication["created_at"]),
                )
                if integration is None:
                    proof = None
                else:
                    proof["canonical_integration_evidence_id"] = integration["id"]
                    proof["canonical_tip_sha"] = integration["canonical_tip_sha"]
            if proof is not None and task_state == "completed":
                terminal_at = _canonical_timestamp(
                    str(publication["created_at"]),
                    "legacy publication created_at",
                )
                return self._publication_endpoint(
                    outcome="succeeded",
                    terminal_at=terminal_at,
                    proof=proof,
                )

        if task_state in {"completed", "failed", "cancelled"}:
            raw_terminal_at = task_completed_at or task_updated_at
            if raw_terminal_at:
                failure = (
                    "legacy_task_completed_without_publication_proof"
                    if task_state == "completed"
                    else "legacy_task_%s" % task_state
                )
                return self._publication_endpoint(
                    outcome="failed",
                    terminal_at=_canonical_timestamp(
                        raw_terminal_at, "legacy task terminal_at"
                    ),
                    failure_class=failure,
                )
        return self._publication_endpoint(
            outcome="censored",
            censoring_reason="canonical_publication_not_yet_observed",
        )

    def _canonical_integration_proof(
        self, task_id: str, *, published_at: str
    ) -> Optional[JsonDict]:
        publication_clock = _parse_timestamp(
            published_at, "legacy publication created_at"
        )
        rows = self.store.query_all(
            "SELECT id, uri, metadata, created_at FROM evidence "
            "WHERE task_id = ? AND kind = 'test' "
            "ORDER BY created_at, id",
            (task_id,),
        )
        valid: list[tuple[datetime, JsonDict]] = []
        for row in rows:
            metadata = json_loads(row["metadata"], {})
            verification = (
                metadata.get("verification")
                if isinstance(metadata, Mapping)
                else None
            )
            integration = (
                verification.get("canonical_integration")
                if isinstance(verification, Mapping)
                else None
            )
            canonical_tip_sha = str(
                integration.get("canonical_tip_sha")
                if isinstance(integration, Mapping)
                else ""
            ).strip()
            if (
                isinstance(integration, Mapping)
                and integration.get("schema") == "mac.canonical_integration.v1"
                and integration.get("status") == "pass"
                and integration.get("contains_reviewed_head") is True
                and integration.get("remote_verified") is True
                and len(canonical_tip_sha) in {40, 64}
                and all(value in "0123456789abcdef" for value in canonical_tip_sha)
                and str(row["uri"])
                == "ledger://canonical-integration/%s/%s"
                % (task_id, canonical_tip_sha)
                and _parse_timestamp(
                    str(row["created_at"]),
                    "canonical integration proof created_at",
                )
                <= publication_clock
            ):
                valid.append(
                    (
                        _parse_timestamp(
                            str(row["created_at"]),
                            "canonical integration proof created_at",
                        ),
                        {
                            "id": str(row["id"]),
                            "canonical_tip_sha": canonical_tip_sha,
                        },
                    )
                )
        return max(valid, key=lambda item: item[0])[1] if valid else None

    def _managed_publication_endpoint(
        self,
        *,
        package_id: Optional[str],
        package: Optional[Mapping[str, Any]],
        finalization: Optional[Mapping[str, Any]],
    ) -> JsonDict:
        """Resolve managed treatment at product finalization or final rejection."""

        candidates: list[JsonDict] = []
        if finalization is not None:
            candidates.append(
                self._publication_endpoint(
                    outcome="succeeded",
                    terminal_at=_canonical_timestamp(
                        str(finalization["finalized_at"]),
                        "managed publication finalized_at",
                    ),
                    proof={
                        "type": "managed_publication_finalization",
                        "id": str(finalization["id"]),
                    },
                )
            )
        if package is not None:
            state = str(package["state"])
            if state in {"failed", "cancelled"} or (
                state == "completed" and finalization is None
            ):
                raw_terminal_at = package["completed_at"] or package["updated_at"]
                candidates.append(
                    self._publication_endpoint(
                        outcome="failed",
                        terminal_at=_canonical_timestamp(
                            str(raw_terminal_at), "managed package terminal_at"
                        ),
                        failure_class=(
                            "managed_package_completed_without_finalization"
                            if state == "completed"
                            else "managed_package_%s" % state
                        ),
                    )
                )
        if package_id:
            certification_rejection = self.store.query_one(
                "SELECT job.id AS job_id, job.completed_at, "
                "certification.id AS certification_id, receipt.id AS receipt_id, "
                "batch.metadata AS batch_metadata "
                "FROM work_package_certification_jobs AS job "
                "JOIN work_package_certifications AS certification "
                "ON certification.id = job.certification_id "
                "JOIN work_package_integration_batches AS batch "
                "ON batch.id = job.batch_id "
                "JOIN work_package_controller_station_receipts AS receipt "
                "ON receipt.certification_job_id = job.id "
                "AND receipt.certification_id = certification.id "
                "WHERE job.package_id = ? AND job.state = 'failed' "
                "AND certification.status = 'failed' "
                "AND batch.state = 'rejected' "
                "AND receipt.station_kind = 'certification' "
                "AND receipt.outcome = 'rejected' "
                "ORDER BY job.completed_at, job.id LIMIT 1",
                (package_id,),
            )
            if certification_rejection is not None:
                batch_metadata = json_loads(
                    certification_rejection["batch_metadata"], {}
                )
                rejection = (
                    batch_metadata.get("product_rejection")
                    if isinstance(batch_metadata, Mapping)
                    else None
                )
                if (
                    isinstance(rejection, Mapping)
                    and rejection.get("status") == "completed"
                    and rejection.get("andon_recorded") is True
                    and rejection.get("wip_disposition") == "quarantined"
                    and rejection.get("certification_id")
                    == certification_rejection["certification_id"]
                    and rejection.get("controller_station_receipt_id")
                    == certification_rejection["receipt_id"]
                ):
                    candidates.append(
                        self._publication_endpoint(
                            outcome="failed",
                            terminal_at=_canonical_timestamp(
                                str(certification_rejection["completed_at"]),
                                "certification rejection completed_at",
                            ),
                            failure_class="managed_certification_rejected_final",
                            proof={
                                "type": "managed_certification_rejection",
                                "id": str(certification_rejection["receipt_id"]),
                                "certification_job_id": str(
                                    certification_rejection["job_id"]
                                ),
                            },
                        )
                    )
            exhausted = self._exhausted_candidate_rejection(package_id)
            if exhausted is not None:
                candidates.append(exhausted)
        if candidates:
            return min(
                candidates,
                key=lambda item: _parse_timestamp(
                    str(item["terminal_at"]), "managed endpoint terminal_at"
                ),
            )
        return self._publication_endpoint(
            outcome="censored",
            censoring_reason=(
                "managed_package_not_materialized"
                if package_id is None
                else "canonical_publication_not_yet_observed"
            ),
        )

    def _exhausted_candidate_rejection(
        self, package_id: str
    ) -> Optional[JsonDict]:
        rows = self.store.query_all(
            "SELECT id, detail, created_at FROM work_package_history "
            "WHERE package_id = ? AND event_type = 'work_package.candidate_rejected' "
            "ORDER BY created_at, id",
            (package_id,),
        )
        for row in rows:
            detail = json_loads(row["detail"], {})
            try:
                remaining = int(detail.get("remaining_rework_cycles"))
            except (AttributeError, TypeError, ValueError):
                remaining = -1
            if (
                isinstance(detail, Mapping)
                and detail.get("schema")
                == "mac.work_package.candidate_rejection.v1"
                and detail.get("retry_staged") is False
                and remaining == 0
                and str(detail.get("candidate_id") or "").strip()
            ):
                return self._publication_endpoint(
                    outcome="failed",
                    terminal_at=_canonical_timestamp(
                        str(row["created_at"]),
                        "candidate rejection created_at",
                    ),
                    failure_class="managed_candidate_rework_exhausted",
                    proof={
                        "type": "managed_exhausted_candidate_rejection",
                        "id": str(row["id"]),
                        "candidate_id": str(detail["candidate_id"]),
                    },
                )
        return None

    @staticmethod
    def _publication_endpoint(
        *,
        outcome: str,
        terminal_at: Optional[str] = None,
        proof: Optional[Mapping[str, Any]] = None,
        failure_class: Optional[str] = None,
        censoring_reason: Optional[str] = None,
    ) -> JsonDict:
        terminal = outcome in {"succeeded", "failed"}
        return {
            "outcome": outcome,
            "terminal": terminal,
            "success": (outcome == "succeeded") if terminal else None,
            "terminal_at": terminal_at,
            "proof": dict(proof) if proof is not None else None,
            "failure_class": failure_class,
            "censoring_reason": censoring_reason,
        }

    def record_measurement_failure(
        self, *, operation: str, error: BaseException, observed_at: Optional[str] = None
    ) -> JsonDict:
        """Increment a direct health counter without invoking telemetry again."""

        operation_value = _required(operation, "measurement failure operation", 128)
        error_type = type(error).__name__
        fingerprint = (
            "sha256:%s"
            % hashlib.sha256(
                (error_type + "\x00" + str(error)).encode("utf-8", errors="replace")
            ).hexdigest()
        )
        timestamp = _canonical_timestamp(
            observed_at or utcnow(), "measurement failure observed_at"
        )
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO work_package_telemetry_health ("
                "singleton_key, failure_count, last_failure_operation, "
                "last_error_type, last_error_fingerprint, last_failed_at, "
                "last_success_at, updated_at"
                ") VALUES (?, 1, ?, ?, ?, ?, NULL, ?) "
                "ON CONFLICT(singleton_key) DO UPDATE SET "
                "failure_count = work_package_telemetry_health.failure_count + 1, "
                "last_failure_operation = excluded.last_failure_operation, "
                "last_error_type = excluded.last_error_type, "
                "last_error_fingerprint = excluded.last_error_fingerprint, "
                "last_failed_at = excluded.last_failed_at, updated_at = excluded.updated_at",
                (
                    "pipeline",
                    operation_value,
                    error_type,
                    fingerprint,
                    timestamp,
                    timestamp,
                ),
            )
            row = conn.execute(
                "SELECT * FROM work_package_telemetry_health WHERE singleton_key = ?",
                ("pipeline",),
            ).fetchone()
        assert row is not None
        return self._measurement_health(row)

    def record_measurement_success(self, *, observed_at: Optional[str] = None) -> None:
        timestamp = _canonical_timestamp(
            observed_at or utcnow(), "measurement success observed_at"
        )
        self.store.execute(
            "INSERT INTO work_package_telemetry_health ("
            "singleton_key, failure_count, last_failure_operation, last_error_type, "
            "last_error_fingerprint, last_failed_at, last_success_at, updated_at"
            ") VALUES (?, 0, '', '', '', NULL, ?, ?) "
            "ON CONFLICT(singleton_key) DO UPDATE SET "
            "last_success_at = excluded.last_success_at, updated_at = excluded.updated_at",
            ("pipeline", timestamp, timestamp),
        )

    def _queue_started_at(
        self,
        conn: Any,
        *,
        package_id: str,
        station: str,
        batch_id: str,
        job_id: str,
        fallback: str,
    ) -> str:
        if station == "admission":
            row = conn.execute(
                "SELECT created_at AS queued_at FROM work_packages WHERE id = ?",
                (package_id,),
            ).fetchone()
        elif station == "integration" and batch_id:
            row = conn.execute(
                "SELECT created_at AS queued_at FROM work_package_integration_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        elif station == "certification" and job_id:
            row = conn.execute(
                "SELECT created_at AS queued_at FROM work_package_certification_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        elif station == "landing" and batch_id:
            row = conn.execute(
                "SELECT created_at AS queued_at FROM work_package_certifications "
                "WHERE batch_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
        elif station == "finalization" and batch_id:
            row = conn.execute(
                "SELECT recorded_at AS queued_at FROM work_package_landing_receipts "
                "WHERE batch_id = ? ORDER BY recorded_at DESC, id DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
        else:
            row = None
        if row is None and station in {"integration", "certification"}:
            row = conn.execute(
                "SELECT updated_at AS queued_at FROM work_packages WHERE id = ?",
                (package_id,),
            ).fetchone()
        return str(row["queued_at"] if row is not None else fallback)

    @staticmethod
    def _classify_pipeline_outcome(
        *, operation: str, status: str, code: str, detail: Mapping[str, Any]
    ) -> tuple[str, str]:
        text = " ".join(
            str(value or "").lower()
            for value in (
                operation,
                status,
                code,
                detail.get("reason"),
                detail.get("error_type"),
                detail.get("error"),
                detail.get("station_status"),
            )
        )
        if "stale" in text:
            return (
                "stale",
                "stale_landing" if operation == "landing" else "stale_generation",
            )
        if operation == "certification_rejection" or "rejected" in text:
            return "rejected", "certification_rejected"
        if operation == "controller_run":
            if status == "completed":
                return "succeeded", ""
            if status in {"busy", "completed_with_contention"}:
                return "busy", "contention"
            if status == "blocked":
                return "held", "dependency_hold"
            return "failed", "controller_run_failure"
        if status == "advanced":
            return "succeeded", ""
        if status == "busy":
            return "busy", "contention"
        if status == "deferred":
            return (
                "held",
                "activation_hold" if operation == "release_gate" else "dependency_hold",
            )
        if status == "no_op":
            return "skipped", ""
        error_type = str(detail.get("error_type") or "").strip()
        return "failed", error_type or "station_failure"

    @staticmethod
    def _assignment(row: JsonDict) -> JsonDict:
        row["rollout_revision"] = int(row["rollout_revision"])
        row["detail"] = json_loads(row.get("detail"), {})
        row["schema"] = COHORT_SCHEMA
        return row

    @staticmethod
    def _attempt(row: JsonDict) -> JsonDict:
        for name in (
            "plan_version",
            "epoch",
            "attempt_number",
            "outcome_index",
            "queue_duration_ms",
            "execution_duration_ms",
        ):
            row[name] = int(row[name])
        row["attempted"] = bool(row["attempted"])
        row["detail"] = json_loads(row.get("detail"), {})
        row["schema"] = STATION_ATTEMPT_SCHEMA
        return row

    @staticmethod
    def _controller_outcome(row: JsonDict) -> JsonDict:
        for name in (
            "outcome_index",
            "plan_version",
            "epoch",
            "execution_duration_ms",
        ):
            row[name] = int(row[name])
        row["attempted"] = bool(row["attempted"])
        row["detail"] = json_loads(row.get("detail"), {})
        row["schema"] = CONTROLLER_OUTCOME_SCHEMA
        return row

    @staticmethod
    def _measurement_health(row: Optional[Mapping[str, Any]]) -> JsonDict:
        if row is None:
            return {
                "schema": "mac.work_package.telemetry_health.v1",
                "failure_count": 0,
                "alert": False,
                "last_failure_operation": None,
                "last_error_type": None,
                "last_error_fingerprint": None,
                "last_failed_at": None,
                "last_success_at": None,
                "updated_at": None,
            }
        failure_count = int(row["failure_count"])
        last_failed_at = row["last_failed_at"]
        last_success_at = row["last_success_at"]
        return {
            "schema": "mac.work_package.telemetry_health.v1",
            "failure_count": failure_count,
            "alert": bool(
                last_failed_at
                and (not last_success_at or str(last_failed_at) > str(last_success_at))
            ),
            "last_failure_operation": row["last_failure_operation"] or None,
            "last_error_type": row["last_error_type"] or None,
            "last_error_fingerprint": row["last_error_fingerprint"] or None,
            "last_failed_at": last_failed_at,
            "last_success_at": last_success_at,
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _outcome_link(row: JsonDict) -> JsonDict:
        row["detail"] = json_loads(row.get("detail"), {})
        row["schema"] = FINALIZATION_OUTCOME_SCHEMA
        return row


__all__ = [
    "COHORT_ASSIGNMENT_ALGORITHM",
    "COHORT_SCHEMA",
    "COMPARABLE_OUTCOME_SCHEMA",
    "CONTROLLER_OUTCOME_SCHEMA",
    "ELIGIBILITY_VALUES",
    "FINALIZATION_OUTCOME_SCHEMA",
    "FINALIZATION_OUTCOME_TYPES",
    "STATION_ATTEMPT_SCHEMA",
    "STATIONS",
    "TERMINAL_STATUSES",
    "TREATMENT_ROUTES",
    "WorkPackageTelemetryService",
    "deterministic_cohort_assignment",
]
