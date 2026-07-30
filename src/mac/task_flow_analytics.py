"""Task-throughput analytics derived from the authoritative MAC ledger.

The service has two deliberately different paths:

* ``record_event`` is an O(1) lifecycle hook.  It keeps the current stage
  materialized without scanning task history while a task mutation holds a
  transaction.
* ``report`` performs a bounded, idempotent repair/backfill for tasks whose
  ledger changed after their last materialization.  Historical truth therefore
  converges even after an older hub, a crash, or a newly added stage mapping.

No model calls are made here.  Every KPI is derived from task history, reviews,
publications, leases, and the current fleet snapshot.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from datetime import timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from mac.models import (
    JsonDict,
    TaskFlowOutcome,
    TaskFlowStage,
    TaskState,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
)


_TERMINAL_STATES = {
    TaskState.COMPLETED.value,
    TaskState.FAILED.value,
    TaskState.CANCELLED.value,
}
_MEANINGLESS_PROGRESS_EVENTS = {
    "task.lease_renewed",
    "task.updated",
    "task.memory_recorded",
    "task.nap_summary_recorded",
}


def _seconds(start: str, end: str) -> float:
    return max(0.0, (parse_time(end) - parse_time(start)).total_seconds())


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _distribution(values: Sequence[float]) -> JsonDict:
    numbers = [float(value) for value in values]
    return {
        "count": len(numbers),
        "min_seconds": min(numbers) if numbers else None,
        "p50_seconds": _percentile(numbers, 0.50),
        "p90_seconds": _percentile(numbers, 0.90),
        "p95_seconds": _percentile(numbers, 0.95),
        "max_seconds": max(numbers) if numbers else None,
        "avg_seconds": (sum(numbers) / len(numbers)) if numbers else None,
    }


def _row_dict(row: Any) -> JsonDict:
    if row is None:
        return {}
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {key: row[key] for key in row.keys()}


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\x00".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()
    return "%s_%s" % (prefix, digest[:32])


def _numeric_total(value: Any, names: Iterable[str]) -> float:
    wanted = set(names)
    total = 0.0
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in wanted and isinstance(item, (int, float)):
                total += float(item)
            else:
                total += _numeric_total(item, wanted)
    elif isinstance(value, list):
        for item in value:
            total += _numeric_total(item, wanted)
    return total


class TaskFlowAnalyticsService:
    """Materialize flow spans and generate bounded throughput diagnostics."""

    def __init__(self, store: Any, observability: Optional[Any] = None) -> None:
        self.store = store
        self.observability = observability
        self._dispatch_record_lock = threading.Lock()
        self._unmatched_round_last_at: Dict[str, float] = {}
        self._unmatched_round_interval_seconds = 300.0
        self._dispatch_retention_last_at = 0.0
        self._dispatch_retention_interval_seconds = 3600.0
        self._dispatch_retention_days = 90

    @staticmethod
    def _round_mapping(value: Any) -> JsonDict:
        if isinstance(value, Mapping):
            return dict(value)
        telemetry_dict = getattr(value, "dispatch_telemetry_dict", None)
        if callable(telemetry_dict):
            mapped = telemetry_dict()
            if isinstance(mapped, Mapping):
                return dict(mapped)
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            mapped = to_dict()
            if isinstance(mapped, Mapping):
                return dict(mapped)
        if is_dataclass(value):
            mapped = asdict(value)
            if isinstance(mapped, dict):
                return mapped
        return {}

    @classmethod
    def _round_items(cls, value: Any) -> List[JsonDict]:
        if value is None:
            return []
        if isinstance(value, (str, bytes, Mapping)):
            values = [value]
        else:
            try:
                values = list(value)
            except TypeError:
                values = [value]
        result: List[JsonDict] = []
        for item in values:
            mapped = cls._round_mapping(item)
            if mapped:
                result.append(mapped)
            elif isinstance(item, str):
                result.append({"id": item})
        return result

    @staticmethod
    def _round_item_id(item: Mapping[str, Any], *names: str) -> Optional[str]:
        for name in names:
            value = item.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _bounded_text_bytes(value: Any, *, limit: int = 2048) -> str:
        """Keep diagnostic text useful while enforcing an encoded byte cap."""

        text = str(value)
        encoded = text.encode("utf-8", errors="replace")
        byte_limit = max(128, int(limit))
        if len(encoded) <= byte_limit:
            return text
        marker = b"\n...[truncated]...\n"
        remaining = byte_limit - len(marker)
        head_size = max(1, (remaining * 3) // 4)
        tail_size = max(1, remaining - head_size)
        compact = encoded[:head_size] + marker + encoded[-tail_size:]
        return compact[:byte_limit].decode("utf-8", errors="ignore")

    @classmethod
    def _bounded_round_detail(
        cls, items: Sequence[Mapping[str, Any]]
    ) -> List[JsonDict]:
        """Bound round evidence without dropping aggregate counts."""

        compact: List[JsonDict] = []
        for item in items[:200]:
            value = dict(item)
            proposal = value.get("proposal")
            assignment = value.get("assignment")
            task = assignment.get("task") if isinstance(assignment, Mapping) else None
            agent = assignment.get("agent") if isinstance(assignment, Mapping) else None
            lease = assignment.get("lease") if isinstance(assignment, Mapping) else None
            projected = {
                key: value[key]
                for key in (
                    "id",
                    "task_id",
                    "agent_id",
                    "lease_id",
                    "reason",
                    "retry_with_other_agent",
                )
                if key in value
            }
            if isinstance(proposal, Mapping):
                for key in ("round_id", "task_id", "agent_id", "task_rank", "agent_rank"):
                    if key in proposal:
                        projected.setdefault(key, proposal[key])
            if isinstance(task, Mapping):
                projected.setdefault("task_id", task.get("id"))
                if task.get("project") is not None:
                    projected["project"] = task.get("project")
            if isinstance(agent, Mapping):
                projected.setdefault("agent_id", agent.get("id"))
            if isinstance(lease, Mapping):
                projected.setdefault("lease_id", lease.get("id"))
            for key in ("reason", "error", "message"):
                if key in projected:
                    projected[key] = cls._bounded_text_bytes(projected[key])
            compact.append({key: val for key, val in projected.items() if val is not None})
        return compact

    @staticmethod
    def _unmatched_round_fingerprint(
        *,
        source: str,
        project: Optional[str],
        unmatched: Sequence[Mapping[str, Any]],
        unmatched_count: int,
    ) -> str:
        task_ids = sorted(
            {
                str(item.get("task_id") or item.get("id") or "")
                for item in unmatched
                if str(item.get("task_id") or item.get("id") or "")
            }
        )
        return hashlib.sha256(
            json_dumps(
                {
                    "source": source,
                    "project": project,
                    "unmatched_count": unmatched_count,
                    "task_ids": task_ids,
                }
            ).encode("utf-8")
        ).hexdigest()

    def _unmatched_round_is_throttled(self, fingerprint: str) -> bool:
        now = time.monotonic()
        interval = max(1.0, float(self._unmatched_round_interval_seconds))
        cutoff = now - interval
        self._unmatched_round_last_at = {
            key: observed_at
            for key, observed_at in self._unmatched_round_last_at.items()
            if observed_at >= cutoff
        }
        last_at = self._unmatched_round_last_at.get(fingerprint, 0.0)
        if last_at and now - last_at < interval:
            return True
        self._unmatched_round_last_at[fingerprint] = now
        return False

    def _prune_dispatch_rounds_if_due(self) -> None:
        """Apply dispatch retention from the material-record lifecycle."""

        now_monotonic = time.monotonic()
        interval = max(1.0, float(self._dispatch_retention_interval_seconds))
        if (
            self._dispatch_retention_last_at
            and now_monotonic - self._dispatch_retention_last_at < interval
        ):
            return
        # A backend failure is also throttled; retention is maintenance and
        # must not become a per-round failure loop.
        self._dispatch_retention_last_at = now_monotonic
        cutoff = (
            parse_time(utcnow()) - timedelta(days=max(1, int(self._dispatch_retention_days)))
        ).isoformat(timespec="microseconds")
        self.store.execute(
            "DELETE FROM dispatch_rounds WHERE created_at < ?",
            (cutoff,),
        )

    def _lock_dispatch_mismatch_scope(self, conn: Any, scope: str) -> None:
        """Serialize one mismatch scope, including its first insertion."""

        try:
            backend = str(self.store.backend_identity().get("backend") or "")
        except Exception:
            backend = ""
        if backend != "postgres":
            # SQLiteStore.transaction() uses BEGIN IMMEDIATE, which already
            # serializes the read/modify/write section.
            return
        digest = hashlib.sha256(("dispatch-mismatch:" + scope).encode("utf-8")).digest()
        lock_key = int.from_bytes(digest[:8], "big", signed=True)
        conn.execute("SELECT pg_advisory_xact_lock(?)", (lock_key,))

    def _emit_dispatch_observation(
        self,
        name: str,
        *,
        level: str,
        source: str,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if self.observability is None:
            return
        self.observability.record_log(
            name,
            level=level,
            layer="control_plane",
            source=source,
            subject_type=subject_type,
            subject_id=subject_id,
            detail=dict(detail or {}),
        )

    def _dispatch_rejection_recent(
        self,
        *,
        task_id: Optional[str],
        observed_at: str,
        window_seconds: float = 300.0,
    ) -> bool:
        if task_id is None:
            return False
        since = (
            parse_time(observed_at) - timedelta(seconds=max(1.0, window_seconds))
        ).isoformat(timespec="microseconds")
        return (
            self.store.query_one(
                "SELECT 1 FROM observability_events "
                "WHERE name = ? AND subject_type = ? AND subject_id = ? "
                "AND created_at >= ? LIMIT 1",
                (
                    "dispatcher.v2.selected_claim_rejected",
                    "task",
                    task_id,
                    since,
                ),
            )
            is not None
        )

    def _update_dispatch_mismatch(
        self,
        *,
        scope: str,
        source: str,
        ready_count: int,
        free_capacity: int,
        assignment_count: int,
        stranded_count: int,
        warning_seconds: float,
        critical_seconds: float,
        observed_at: str,
        round_id: str,
    ) -> Optional[JsonDict]:
        """Advance one rate-limited ready-work/free-capacity episode."""

        mismatch = stranded_count > 0
        if not mismatch:
            # The overwhelmingly common idle-poll path should not acquire
            # SQLite's BEGIN IMMEDIATE writer lock.  Re-read under the
            # transaction only when there is actually an active episode to
            # resolve.
            candidate = _row_dict(
                self.store.query_one(
                    "SELECT * FROM dispatch_mismatch_state "
                    "WHERE scope = ? AND resolved_at IS NULL",
                    (scope,),
                )
            )
            if not candidate:
                return None
        event_name = ""
        event_level = "info"
        event_subject_id = ""
        detail: Optional[JsonDict] = None
        with self.store.transaction() as conn:
            self._lock_dispatch_mismatch_scope(conn, scope)
            current_row = conn.execute(
                "SELECT * FROM dispatch_mismatch_state WHERE scope = ?",
                (scope,),
            ).fetchone()
            current = _row_dict(current_row)
            active = bool(current and current.get("resolved_at") is None)
            if current:
                try:
                    stale = parse_time(observed_at) < parse_time(
                        str(current["last_observed_at"])
                    )
                except Exception:
                    stale = observed_at < str(current["last_observed_at"])
                if stale:
                    return {
                        "schema": "mac.dispatch_capacity_mismatch.v1",
                        "episode_id": current["episode_id"],
                        "scope": scope,
                        "active": active,
                        "ignored": True,
                        "reason": "stale_observation",
                        "observed_at": observed_at,
                        "last_observed_at": current["last_observed_at"],
                        "round_id": round_id,
                    }

            if not mismatch:
                if not active:
                    return None
                changed = conn.execute(
                    "UPDATE dispatch_mismatch_state SET resolved_at = ?, "
                    "last_observed_at = ?, updated_at = ? "
                    "WHERE scope = ? AND resolved_at IS NULL "
                    "AND last_observed_at <= ?",
                    (observed_at, observed_at, observed_at, scope, observed_at),
                )
                if changed.rowcount != 1:
                    return {
                        "schema": "mac.dispatch_capacity_mismatch.v1",
                        "episode_id": current["episode_id"],
                        "scope": scope,
                        "active": True,
                        "ignored": True,
                        "reason": "concurrent_newer_observation",
                        "observed_at": observed_at,
                        "last_observed_at": current["last_observed_at"],
                        "round_id": round_id,
                    }
                detail = {
                    "schema": "mac.dispatch_capacity_mismatch.v1",
                    "episode_id": current["episode_id"],
                    "scope": scope,
                    "opened_at": current["opened_at"],
                    "resolved_at": observed_at,
                    "age_seconds": float(current["age_seconds"]),
                    "severity": current["severity"],
                    "reason": current["reason"],
                    "round_id": round_id,
                    "active": False,
                }
                event_name = "dispatcher.v2.ready_capacity_mismatch_resolved"
                event_subject_id = str(current["episode_id"])
            else:
                opened_at = str(current["opened_at"]) if active else observed_at
                age = _seconds(opened_at, observed_at)
                if age >= critical_seconds:
                    severity = "critical"
                elif age >= warning_seconds:
                    severity = "warning"
                else:
                    severity = "pending"
                previous_severity = str(current.get("severity") or "") if active else ""
                episode_id = (
                    str(current["episode_id"])
                    if active
                    else _stable_id("dispatchstrand", scope, observed_at)
                )
                reason = "ready_work_and_free_capacity_unmatched"
                metadata = {
                    "schema": "mac.dispatch_capacity_mismatch.v1",
                    "round_id": round_id,
                    "unmatched_ready_count": max(0, ready_count - assignment_count),
                    "unused_capacity": max(0, free_capacity - assignment_count),
                    "warning_seconds": warning_seconds,
                    "critical_seconds": critical_seconds,
                }
                changed = conn.execute(
                    "INSERT INTO dispatch_mismatch_state ("
                    "scope, episode_id, opened_at, last_observed_at, resolved_at, "
                    "age_seconds, severity, ready_count, free_capacity, assignment_count, "
                    "reason, metadata, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(scope) DO UPDATE SET "
                    "episode_id = excluded.episode_id, opened_at = excluded.opened_at, "
                    "last_observed_at = excluded.last_observed_at, resolved_at = NULL, "
                    "age_seconds = excluded.age_seconds, severity = excluded.severity, "
                    "ready_count = excluded.ready_count, "
                    "free_capacity = excluded.free_capacity, "
                    "assignment_count = excluded.assignment_count, "
                    "reason = excluded.reason, metadata = excluded.metadata, "
                    "updated_at = excluded.updated_at "
                    "WHERE dispatch_mismatch_state.last_observed_at "
                    "<= excluded.last_observed_at",
                    (
                        scope,
                        episode_id,
                        opened_at,
                        observed_at,
                        age,
                        severity,
                        ready_count,
                        free_capacity,
                        assignment_count,
                        reason,
                        json_dumps(metadata),
                        observed_at,
                        observed_at,
                    ),
                )
                if changed.rowcount != 1:
                    latest = _row_dict(
                        conn.execute(
                            "SELECT * FROM dispatch_mismatch_state WHERE scope = ?",
                            (scope,),
                        ).fetchone()
                    )
                    return {
                        "schema": "mac.dispatch_capacity_mismatch.v1",
                        "episode_id": latest.get("episode_id"),
                        "scope": scope,
                        "active": bool(latest and latest.get("resolved_at") is None),
                        "ignored": True,
                        "reason": "concurrent_newer_observation",
                        "observed_at": observed_at,
                        "last_observed_at": latest.get("last_observed_at"),
                        "round_id": round_id,
                    }
                detail = {
                    "schema": "mac.dispatch_capacity_mismatch.v1",
                    "episode_id": episode_id,
                    "scope": scope,
                    "active": True,
                    "opened_at": opened_at,
                    "last_observed_at": observed_at,
                    "age_seconds": age,
                    "severity": severity,
                    "ready_count": ready_count,
                    "free_capacity": free_capacity,
                    "assignment_count": assignment_count,
                    "stranded_count": stranded_count,
                    "reason": reason,
                    **metadata,
                }
                event_subject_id = episode_id
                if not active:
                    event_name = "dispatcher.v2.ready_capacity_mismatch_opened"
                elif severity != previous_severity:
                    event_name = "dispatcher.v2.ready_capacity_mismatch_%s" % severity
                    event_level = "error" if severity == "critical" else "warning"

        if event_name and detail is not None:
            self._emit_dispatch_observation(
                event_name,
                level=event_level,
                source=source,
                subject_type="dispatch_mismatch",
                subject_id=event_subject_id,
                detail=detail,
            )
        return detail

    def record_dispatch_round(
        self,
        result: Any,
        *,
        warning_seconds: float = 300.0,
        critical_seconds: float = 600.0,
    ) -> JsonDict:
        """Serialize material round recording within this hub process."""

        with self._dispatch_record_lock:
            return self._record_dispatch_round_unlocked(
                result,
                warning_seconds=warning_seconds,
                critical_seconds=critical_seconds,
            )

    def _record_dispatch_round_unlocked(
        self,
        result: Any,
        *,
        warning_seconds: float = 300.0,
        critical_seconds: float = 600.0,
    ) -> JsonDict:
        """Retain one authoritative allocator round and its anomalies.

        The dispatcher-v2 allocator calls this exactly once after its lease
        transaction.  The method accepts a mapping or dataclass so allocator
        result evolution does not couple the analytics service to allocator
        internals.
        """

        payload = self._round_mapping(result)
        started_at = str(payload.get("started_at") or utcnow())
        completed_at = str(payload.get("completed_at") or started_at)
        source = str(payload.get("source") or "authoritative-allocator")
        allocator_version = str(
            payload.get("allocator_version") or payload.get("version") or "v2"
        )
        project_value = payload.get("project")
        project = str(project_value) if project_value is not None else None

        assignments = self._round_items(payload.get("assignments"))
        unmatched = self._round_items(
            payload.get("unmatched_tasks") or payload.get("unmatched")
        )
        if not unmatched:
            unmatched = self._round_items(payload.get("unmatched_task_ids"))
        stranded = self._round_items(
            payload.get("stranded_tasks") or payload.get("stranded_task_ids")
        )
        claim_failures = self._round_items(payload.get("claim_failures"))
        ready_items = self._round_items(
            payload.get("ready_tasks")
            or payload.get("candidate_tasks")
            or payload.get("ready_task_ids")
            or payload.get("candidate_task_ids")
            or payload.get("tasks")
        )
        free_items = self._round_items(
            payload.get("free_agents")
            or payload.get("available_agents")
            or payload.get("available_agent_ids")
            or payload.get("agents")
        )
        ready_ids = [
            item_id
            for item in ready_items
            if (
                item_id := self._round_item_id(item, "task_id", "id")
            )
        ]
        if not ready_ids:
            ready_ids = [
                item_id
                for item in assignments + unmatched
                if (
                    item_id := self._round_item_id(item, "task_id", "id")
                )
            ]
        free_agent_ids = [
            item_id
            for item in free_items
            if (
                item_id := self._round_item_id(item, "agent_id", "id")
            )
        ]
        ready_count = max(
            0,
            int(payload.get("ready_count", len(set(ready_ids))) or 0),
        )
        free_capacity = max(
            0,
            int(payload.get("free_capacity", len(set(free_agent_ids))) or 0),
        )
        assignment_count = len(assignments)
        unmatched_count = max(
            len(unmatched),
            int(payload.get("unmatched_count", 0) or 0),
        )
        stranded_count = max(
            len(stranded),
            int(payload.get("stranded_count", 0) or 0),
        )
        claim_failure_count = len(claim_failures)
        false_ready_count = max(
            claim_failure_count,
            int(payload.get("false_ready_count", 0) or 0),
        )
        duration = _seconds(started_at, completed_at)
        round_id = str(
            payload.get("round_id")
            or payload.get("id")
            or _stable_id(
                "dispatchround",
                source,
                started_at,
                completed_at,
                ",".join(sorted(set(ready_ids))),
            )
        )
        detail: JsonDict = {
            "schema": "mac.dispatch_round.v2",
            "round_id": round_id,
            "allocator_version": allocator_version,
            "source": source,
            "project": project,
            "projects": sorted(
                {
                    str(item)
                    for item in (payload.get("projects") or [])
                    if str(item)
                }
            ),
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": duration,
            "ready_count": ready_count,
            "free_capacity": free_capacity,
            "assignment_count": assignment_count,
            "unmatched_count": unmatched_count,
            "stranded_count": stranded_count,
            "claim_failure_count": claim_failure_count,
            "false_ready_count": false_ready_count,
            "ready_task_ids": sorted(set(ready_ids))[:200],
            "free_agent_ids": sorted(set(free_agent_ids))[:200],
            "assignments": self._bounded_round_detail(assignments),
            "unmatched_tasks": self._bounded_round_detail(unmatched),
            "claim_failures": self._bounded_round_detail(claim_failures),
        }

        def advance_mismatch() -> Optional[JsonDict]:
            return self._update_dispatch_mismatch(
                scope=project or "*",
                source=source,
                ready_count=ready_count,
                free_capacity=free_capacity,
                assignment_count=assignment_count,
                stranded_count=stranded_count,
                warning_seconds=float(warning_seconds),
                critical_seconds=float(critical_seconds),
                observed_at=completed_at,
                round_id=round_id,
            )

        # Empty polling rounds are intentionally write-free.  The allocator
        # result is still returned to its caller.  A prior mismatch is the one
        # exception: resolving stale operator-visible state needs one final
        # state write, but never a dispatch-round row.
        material_round = bool(
            assignment_count
            or unmatched_count
            or stranded_count
            or claim_failure_count
        )
        if not material_round:
            return {
                **detail,
                "mismatch": advance_mismatch(),
                "retained": False,
            }
        unmatched_only = bool(
            unmatched_count
            and not assignment_count
            and not stranded_count
            and not claim_failure_count
        )
        if unmatched_only:
            fingerprint = self._unmatched_round_fingerprint(
                source=source,
                project=project,
                unmatched=unmatched,
                unmatched_count=unmatched_count,
            )
            detail["demand_fingerprint"] = fingerprint
            if self._unmatched_round_is_throttled(fingerprint):
                return {
                    **detail,
                    "mismatch": advance_mismatch(),
                    "retained": False,
                    "deduplicated": True,
                }
        self.store.execute(
            "INSERT INTO dispatch_rounds ("
            "id, allocator_version, source, project, started_at, completed_at, "
            "duration_seconds, ready_count, free_capacity, assignment_count, "
            "unmatched_count, claim_failure_count, false_ready_count, detail, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET completed_at = excluded.completed_at, "
            "duration_seconds = excluded.duration_seconds, "
            "ready_count = excluded.ready_count, "
            "free_capacity = excluded.free_capacity, "
            "assignment_count = excluded.assignment_count, "
            "unmatched_count = excluded.unmatched_count, "
            "claim_failure_count = excluded.claim_failure_count, "
            "false_ready_count = excluded.false_ready_count, "
            "detail = excluded.detail",
            (
                round_id,
                allocator_version,
                source,
                project,
                started_at,
                completed_at,
                duration,
                ready_count,
                free_capacity,
                assignment_count,
                unmatched_count,
                claim_failure_count,
                false_ready_count,
                json_dumps(detail),
                completed_at,
            ),
        )
        for failure in claim_failures:
            task_id = self._round_item_id(failure, "task_id", "id")
            if self._dispatch_rejection_recent(
                task_id=task_id,
                observed_at=completed_at,
            ):
                continue
            self._emit_dispatch_observation(
                "dispatcher.v2.selected_claim_rejected",
                level="error",
                source=source,
                subject_type="task" if task_id else "dispatch_round",
                subject_id=task_id or round_id,
                detail={
                    "schema": "mac.dispatch_claim_rejection.v1",
                    "round_id": round_id,
                    **(self._bounded_round_detail([failure]) or [{}])[0],
                },
            )
        mismatch = advance_mismatch()
        retention_error = None
        try:
            self._prune_dispatch_rounds_if_due()
        except Exception as exc:  # Retention cannot invalidate allocator output.
            retention_error = self._bounded_text_bytes(
                "%s:%s" % (exc.__class__.__name__, str(exc))
            )
        return {
            **detail,
            "mismatch": mismatch,
            "retained": True,
            "retention_error": retention_error,
        }

    @staticmethod
    def _stage_for_event(
        event_type: str,
        from_state: Optional[str],
        to_state: Optional[str],
        detail: Mapping[str, Any],
    ) -> Optional[str]:
        if event_type == "task.created":
            return (
                TaskFlowStage.READY_QUEUE.value
                if to_state == TaskState.OPEN.value
                else TaskFlowStage.INTAKE.value
            )
        if event_type == "task.claimed":
            return TaskFlowStage.CLAIM_TO_START.value
        if event_type in {"task.lease_expired", "task.lease_released"}:
            return TaskFlowStage.READY_QUEUE.value
        if event_type == "task.review_requested":
            return TaskFlowStage.REVIEW_QUEUE.value
        if event_type == "task.review_claimed":
            return TaskFlowStage.REVIEW.value
        if event_type == "task.review_completed":
            status = str(detail.get("status") or "").lower()
            if status == "approved":
                return TaskFlowStage.INTEGRATION_QUEUE.value
            if status == "rejected":
                return TaskFlowStage.EXECUTION.value
            return None
        if event_type == "task.published":
            return TaskFlowStage.PUBLICATION.value
        if event_type != "task.transitioned":
            return None
        return {
            TaskState.OPEN.value: TaskFlowStage.READY_QUEUE.value,
            TaskState.WAITING.value: TaskFlowStage.INTAKE.value,
            TaskState.BLOCKED.value: TaskFlowStage.INTAKE.value,
            TaskState.CLAIMED.value: TaskFlowStage.CLAIM_TO_START.value,
            TaskState.RUNNING.value: TaskFlowStage.EXECUTION.value,
            TaskState.NEEDS_REVIEW.value: TaskFlowStage.REVIEW_QUEUE.value,
            TaskState.REVIEWING.value: TaskFlowStage.REVIEW_QUEUE.value,
        }.get(str(to_state or ""))

    @staticmethod
    def _outcome_for_state(state: Optional[str]) -> str:
        return {
            TaskState.COMPLETED.value: TaskFlowOutcome.COMPLETED.value,
            TaskState.FAILED.value: TaskFlowOutcome.FAILED.value,
            TaskState.CANCELLED.value: TaskFlowOutcome.CANCELLED.value,
        }.get(str(state or ""), TaskFlowOutcome.PENDING.value)

    @staticmethod
    def _source(writer: Optional[Any], store: Any) -> Any:
        return writer if writer is not None else store

    def _close_open_spans(
        self,
        source: Any,
        *,
        task_id: str,
        when: str,
        keep_attempt: Optional[int] = None,
        keep_stage: Optional[str] = None,
        outcome: str = TaskFlowOutcome.COMPLETED.value,
    ) -> None:
        rows = source.execute(
            "SELECT * FROM task_flow_spans "
            "WHERE task_id = ? AND ended_at IS NULL",
            (task_id,),
        ).fetchall()
        for row in rows:
            item = _row_dict(row)
            if (
                keep_attempt == int(item["attempt"])
                and keep_stage == str(item["stage"])
            ):
                continue
            metadata = json_loads(item.get("metadata"), {})
            accumulated = float(metadata.get("accumulated_duration_seconds") or 0.0)
            duration = accumulated + _seconds(str(item["started_at"]), when)
            source.execute(
                "UPDATE task_flow_spans SET ended_at = ?, duration_seconds = ?, "
                "outcome = ?, metadata = ?, updated_at = ? WHERE id = ?",
                (
                    when,
                    duration,
                    outcome,
                    json_dumps(
                        {
                            **metadata,
                            "accumulated_duration_seconds": duration,
                        }
                    ),
                    when,
                    item["id"],
                ),
            )

    def _open_stage(
        self,
        source: Any,
        *,
        task_id: str,
        project: str,
        attempt: int,
        stage: str,
        when: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        existing = source.execute(
            "SELECT * FROM task_flow_spans "
            "WHERE task_id = ? AND attempt = ? AND stage = ?",
            (task_id, attempt, stage),
        ).fetchone()
        if existing is None:
            source.execute(
                "INSERT INTO task_flow_spans ("
                "id, task_id, project, attempt, stage, started_at, ended_at, "
                "duration_seconds, outcome, metadata, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
                (
                    _stable_id("flow", task_id, attempt, stage),
                    task_id,
                    project,
                    attempt,
                    stage,
                    when,
                    TaskFlowOutcome.PENDING.value,
                    json_dumps(ensure_json_object(metadata)),
                    when,
                    when,
                ),
            )
            return
        item = _row_dict(existing)
        if item.get("ended_at") is None:
            return
        old_metadata = json_loads(item.get("metadata"), {})
        accumulated = float(item.get("duration_seconds") or 0.0)
        source.execute(
            "UPDATE task_flow_spans SET started_at = ?, ended_at = NULL, "
            "duration_seconds = NULL, outcome = ?, metadata = ?, updated_at = ? "
            "WHERE id = ?",
            (
                when,
                TaskFlowOutcome.PENDING.value,
                json_dumps(
                    {
                        **old_metadata,
                        **ensure_json_object(metadata),
                        "accumulated_duration_seconds": accumulated,
                        "segment_count": int(old_metadata.get("segment_count") or 1)
                        + 1,
                    }
                ),
                when,
                item["id"],
            ),
        )

    def _upsert_completion_from_spans(
        self,
        source: Any,
        *,
        task_id: str,
        project: str,
        attempt: int,
        outcome: str,
        ended_at: Optional[str],
        updated_at: str,
    ) -> None:
        span_rows = source.execute(
            "SELECT * FROM task_flow_spans WHERE task_id = ? AND attempt = ? "
            "ORDER BY started_at, stage",
            (task_id, attempt),
        ).fetchall()
        if not span_rows:
            return
        spans = [_row_dict(row) for row in span_rows]
        stage_durations = {
            str(row["stage"]): float(row["duration_seconds"])
            for row in spans
            if row.get("duration_seconds") is not None
        }
        started_at = min(str(row["started_at"]) for row in spans)
        duration = _seconds(started_at, ended_at) if ended_at else None
        publication = source.execute(
            "SELECT content_hash FROM publications WHERE task_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        review_count_row = source.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        publication_sha = (
            str(publication["content_hash"])
            if publication is not None and publication["content_hash"]
            else None
        )
        source.execute(
            "INSERT INTO task_completions ("
            "id, task_id, project, attempt, started_at, ended_at, "
            "duration_seconds, outcome, publication_sha, main_sha, route_count, "
            "token_count, cost_count, review_count, rebase_count, test_count, "
            "per_stage_durations, metadata, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0, 0, ?, 0, 0, ?, ?, ?, ?) "
            "ON CONFLICT(task_id, attempt) DO UPDATE SET "
            "project = excluded.project, started_at = excluded.started_at, "
            "ended_at = excluded.ended_at, duration_seconds = excluded.duration_seconds, "
            "outcome = excluded.outcome, publication_sha = excluded.publication_sha, "
            "review_count = excluded.review_count, "
            "per_stage_durations = excluded.per_stage_durations, "
            "metadata = excluded.metadata, updated_at = excluded.updated_at",
            (
                _stable_id("completion", task_id, attempt),
                task_id,
                project,
                attempt,
                started_at,
                ended_at,
                duration,
                outcome,
                publication_sha,
                int(review_count_row["n"]) if review_count_row is not None else 0,
                json_dumps(stage_durations),
                json_dumps({"schema": "mac.task_completion.materialization.v1"}),
                started_at,
                updated_at,
            ),
        )

    def record_event(
        self,
        *,
        task_id: str,
        event_type: str,
        from_state: Optional[str],
        to_state: Optional[str],
        detail: Mapping[str, Any],
        when: str,
        writer: Optional[Any] = None,
    ) -> None:
        """Apply one lifecycle event without scanning the task's history."""

        source = self._source(writer, self.store)
        task = source.execute(
            "SELECT project, state, attempt_count, created_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task is None:
            return
        task_item = _row_dict(task)
        project = str(task_item.get("project") or "unassigned")
        attempt = max(1, int(task_item.get("attempt_count") or 0))
        if event_type in {"task.lease_expired", "task.lease_released"}:
            attempt += 1
        stage = self._stage_for_event(event_type, from_state, to_state, detail)
        terminal_state = (
            str(to_state)
            if event_type == "task.transitioned" and to_state in _TERMINAL_STATES
            else None
        )
        if terminal_state:
            outcome = self._outcome_for_state(terminal_state)
            self._close_open_spans(
                source,
                task_id=task_id,
                when=when,
                outcome=outcome,
            )
            self._open_stage(
                source,
                task_id=task_id,
                project=project,
                attempt=attempt,
                stage=TaskFlowStage.FINALIZATION.value,
                when=when,
                metadata={"terminal_state": terminal_state},
            )
            self._close_open_spans(
                source,
                task_id=task_id,
                when=when,
                outcome=outcome,
            )
            self._upsert_completion_from_spans(
                source,
                task_id=task_id,
                project=project,
                attempt=attempt,
                outcome=outcome,
                ended_at=when,
                updated_at=when,
            )
            return
        if stage is None:
            return
        self._close_open_spans(
            source,
            task_id=task_id,
            when=when,
            keep_attempt=attempt,
            keep_stage=stage,
        )
        self._open_stage(
            source,
            task_id=task_id,
            project=project,
            attempt=attempt,
            stage=stage,
            when=when,
            metadata={"source_event": event_type},
        )

    def rebuild_task(self, task_id: str, *, observed_at: Optional[str] = None) -> None:
        """Idempotently derive every span for one task from canonical history."""

        now = observed_at or utcnow()
        task_row = self.store.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if task_row is None:
            return
        task = _row_dict(task_row)
        history = [
            _row_dict(row)
            for row in self.store.query_all(
                "SELECT * FROM task_history WHERE task_id = ? "
                "ORDER BY created_at, id",
                (task_id,),
            )
        ]
        project = str(task.get("project") or "unassigned")
        current_attempt = 1
        claim_count = 0
        current: Optional[JsonDict] = None
        segments: List[JsonDict] = []

        def close_current(when: str, outcome: str = TaskFlowOutcome.COMPLETED.value) -> None:
            nonlocal current
            if current is None:
                return
            current["ended_at"] = when
            current["duration_seconds"] = _seconds(str(current["started_at"]), when)
            current["outcome"] = outcome
            segments.append(current)
            current = None

        def start_stage(stage: str, when: str, attempt: int, source_event: str) -> None:
            nonlocal current
            if (
                current is not None
                and current["stage"] == stage
                and int(current["attempt"]) == attempt
            ):
                return
            close_current(when)
            current = {
                "attempt": attempt,
                "stage": stage,
                "started_at": when,
                "ended_at": None,
                "duration_seconds": None,
                "outcome": TaskFlowOutcome.PENDING.value,
                "source_events": [source_event],
            }

        initial_stage = (
            TaskFlowStage.READY_QUEUE.value
            if str(task.get("state")) == TaskState.OPEN.value or history
            else TaskFlowStage.INTAKE.value
        )
        start_stage(initial_stage, str(task["created_at"]), 1, "task.created.inferred")
        terminal_outcome: Optional[str] = None
        for event in history:
            event_type = str(event.get("event_type") or "")
            detail = json_loads(event.get("detail"), {})
            from_state = event.get("from_state")
            to_state = event.get("to_state")
            when = str(event["created_at"])
            if event_type == "task.claimed":
                claim_count += 1
                current_attempt = max(1, claim_count)
            elif event_type in {"task.lease_expired", "task.lease_released"}:
                current_attempt = max(1, claim_count + 1)
            elif (
                event_type == "task.transitioned"
                and to_state == TaskState.OPEN.value
                and from_state in _TERMINAL_STATES
            ):
                current_attempt = max(current_attempt + 1, claim_count + 1)
            if (
                event_type == "task.transitioned"
                and str(to_state or "") in _TERMINAL_STATES
            ):
                terminal_outcome = self._outcome_for_state(str(to_state))
                close_current(when, terminal_outcome)
                start_stage(
                    TaskFlowStage.FINALIZATION.value,
                    when,
                    current_attempt,
                    event_type,
                )
                close_current(when, terminal_outcome)
                continue
            stage = self._stage_for_event(
                event_type,
                str(from_state) if from_state is not None else None,
                str(to_state) if to_state is not None else None,
                detail,
            )
            if stage is not None:
                start_stage(stage, when, current_attempt, event_type)
            elif current is not None and event_type not in _MEANINGLESS_PROGRESS_EVENTS:
                current["source_events"].append(event_type)

        if current is not None:
            if str(task.get("state")) in _TERMINAL_STATES:
                close_current(
                    str(task.get("completed_at") or task.get("updated_at") or now),
                    self._outcome_for_state(str(task.get("state"))),
                )
            else:
                segments.append(current)

        grouped: Dict[Tuple[int, str], List[JsonDict]] = defaultdict(list)
        for segment in segments:
            grouped[(int(segment["attempt"]), str(segment["stage"]))].append(segment)

        with self.store.transaction() as conn:
            conn.execute("DELETE FROM task_flow_spans WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM task_completions WHERE task_id = ?", (task_id,))
            for (attempt, stage), items in grouped.items():
                pending = items[-1]["ended_at"] is None
                duration_values = [
                    float(item["duration_seconds"])
                    for item in items
                    if item["duration_seconds"] is not None
                ]
                duration = None if pending else sum(duration_values)
                started_at = str(items[-1]["started_at"] if pending else items[0]["started_at"])
                ended_at = None if pending else str(items[-1]["ended_at"])
                outcome = (
                    TaskFlowOutcome.PENDING.value
                    if pending
                    else str(items[-1]["outcome"])
                )
                conn.execute(
                    "INSERT INTO task_flow_spans ("
                    "id, task_id, project, attempt, stage, started_at, ended_at, "
                    "duration_seconds, outcome, metadata, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _stable_id("flow", task_id, attempt, stage),
                        task_id,
                        project,
                        attempt,
                        stage,
                        started_at,
                        ended_at,
                        duration,
                        outcome,
                        json_dumps(
                            {
                                "schema": "mac.task_flow_span.derivation.v1",
                                "segment_count": len(items),
                                "accumulated_duration_seconds": sum(duration_values),
                                "source_events": sorted(
                                    {
                                        event
                                        for item in items
                                        for event in item["source_events"]
                                        if event
                                    }
                                ),
                            }
                        ),
                        str(items[0]["started_at"]),
                        now,
                    ),
                )

            attempts = sorted({int(item["attempt"]) for item in segments})
            for index, attempt in enumerate(attempts):
                attempt_segments = [
                    item for item in segments if int(item["attempt"]) == attempt
                ]
                start = min(str(item["started_at"]) for item in attempt_segments)
                is_latest = index == len(attempts) - 1
                if is_latest and str(task.get("state")) not in _TERMINAL_STATES:
                    end = None
                    outcome = TaskFlowOutcome.PENDING.value
                else:
                    closed = [
                        str(item["ended_at"])
                        for item in attempt_segments
                        if item["ended_at"] is not None
                    ]
                    end = max(closed) if closed else now
                    outcome = (
                        terminal_outcome
                        if is_latest and terminal_outcome
                        else TaskFlowOutcome.FAILED.value
                    )
                stage_durations: Dict[str, float] = defaultdict(float)
                for item in attempt_segments:
                    if item["duration_seconds"] is not None:
                        stage_durations[str(item["stage"])] += float(
                            item["duration_seconds"]
                        )
                attempt_history = [
                    event
                    for event in history
                    if str(event["created_at"]) >= start
                    and (end is None or str(event["created_at"]) <= end)
                ]
                details = [json_loads(event.get("detail"), {}) for event in attempt_history]
                route_count = sum(
                    1
                    for event in attempt_history
                    if "route" in str(event.get("event_type") or "")
                )
                token_count = int(
                    sum(
                        _numeric_total(
                            detail,
                            {"token_count", "total_tokens", "tokens"},
                        )
                        for detail in details
                    )
                )
                cost_count = sum(
                    _numeric_total(
                        detail,
                        {"cost", "cost_count", "estimated_cost", "total_cost"},
                    )
                    for detail in details
                )
                rebase_count = sum(
                    1
                    for event in attempt_history
                    if "rebase" in str(event.get("event_type") or "").lower()
                )
                test_count = sum(
                    1
                    for event in attempt_history
                    if "test" in str(event.get("event_type") or "").lower()
                    or "test" in json_dumps(json_loads(event.get("detail"), {})).lower()
                )
                publication = conn.execute(
                    "SELECT content_hash FROM publications WHERE task_id = ? "
                    "AND created_at >= ? ORDER BY created_at DESC, id DESC LIMIT 1",
                    (task_id, start),
                ).fetchone()
                review_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM reviews WHERE task_id = ? "
                    "AND created_at >= ?",
                    (task_id, start),
                ).fetchone()
                conn.execute(
                    "INSERT INTO task_completions ("
                    "id, task_id, project, attempt, started_at, ended_at, "
                    "duration_seconds, outcome, publication_sha, main_sha, "
                    "route_count, token_count, cost_count, review_count, "
                    "rebase_count, test_count, per_stage_durations, metadata, "
                    "created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _stable_id("completion", task_id, attempt),
                        task_id,
                        project,
                        attempt,
                        start,
                        end,
                        _seconds(start, end) if end else None,
                        outcome,
                        (
                            str(publication["content_hash"])
                            if publication is not None and publication["content_hash"]
                            else None
                        ),
                        route_count,
                        token_count,
                        cost_count,
                        int(review_count["n"]) if review_count is not None else 0,
                        rebase_count,
                        test_count,
                        json_dumps(dict(stage_durations)),
                        json_dumps(
                            {
                                "schema": "mac.task_completion.derivation.v1",
                                "history_event_count": len(attempt_history),
                            }
                        ),
                        start,
                        now,
                    ),
                )

    def refresh_stale(
        self,
        *,
        project: Optional[str] = None,
        limit: int = 100,
        observed_at: Optional[str] = None,
    ) -> JsonDict:
        """Repair a bounded page whose ledger is newer than its flow materialization."""

        clauses: List[str] = []
        params: List[Any] = []
        if project is not None:
            clauses.append("t.project = ?")
            params.append(project)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(0, min(int(limit), 500)))
        rows = self.store.query_all(
            "SELECT t.id FROM tasks t "
            "LEFT JOIN (SELECT task_id, MAX(updated_at) AS materialized_at "
            "FROM task_flow_spans GROUP BY task_id) f ON f.task_id = t.id "
            "%s AND (f.materialized_at IS NULL OR t.updated_at > f.materialized_at) "
            "ORDER BY t.updated_at DESC, t.id LIMIT ?" % (
                where if where else "WHERE 1 = 1"
            ),
            tuple(params),
        )
        refreshed: List[str] = []
        for row in rows:
            task_id = str(row["id"])
            self.rebuild_task(task_id, observed_at=observed_at)
            refreshed.append(task_id)
        return {
            "schema": "mac.task_flow_refresh.v1",
            "refreshed_count": len(refreshed),
            "task_ids": refreshed,
            "limit": max(0, min(int(limit), 500)),
        }

    @staticmethod
    def _stranding_reason(
        stage: str,
        dispatch: Optional[Mapping[str, Any]],
    ) -> str:
        if stage == TaskFlowStage.READY_QUEUE.value:
            if dispatch is not None:
                reasons = dispatch.get("unclaimed_reasons") or []
                if reasons:
                    first = reasons[0]
                    if isinstance(first, Mapping) and first.get("code"):
                        return str(first["code"])
                if dispatch.get("dispatchable"):
                    return "dispatcher_backlog"
            return "ready_unclaimed"
        return {
            TaskFlowStage.INTAKE.value: "dependency_or_intake_wait",
            TaskFlowStage.CLAIM_TO_START.value: "executor_start_delay",
            TaskFlowStage.EXECUTION.value: "executor_no_meaningful_progress",
            TaskFlowStage.REVIEW_QUEUE.value: "reviewer_queue_delay",
            TaskFlowStage.REVIEW.value: "review_execution_delay",
            TaskFlowStage.INTEGRATION_QUEUE.value: "integration_queue_delay",
            TaskFlowStage.INTEGRATION_TEST.value: "integration_test_delay",
            TaskFlowStage.PUBLICATION.value: "publication_delay",
            TaskFlowStage.CI_FOLLOW_UP.value: "ci_follow_up_delay",
            TaskFlowStage.FINALIZATION.value: "finalization_delay",
        }.get(stage, "unknown_stage_delay")

    def record_contention(
        self,
        *,
        task_id: Optional[str],
        project: Optional[str],
        attempt: Optional[int],
        stage: str,
        resource_class: str,
        resource_key: str,
        reason: str,
        peer_task_ids: Optional[Sequence[str]] = None,
        wait_started_at: Optional[str] = None,
        wait_ended_at: Optional[str] = None,
        outcome: str = "observed",
        metadata: Optional[Mapping[str, Any]] = None,
        observed_at: Optional[str] = None,
    ) -> JsonDict:
        """Record a secret-free collision over a shared resource."""

        now = observed_at or utcnow()
        started = wait_started_at or now
        ended = wait_ended_at
        peers = sorted({str(item) for item in (peer_task_ids or []) if str(item)})
        resource_digest = hashlib.sha256(resource_key.encode("utf-8")).hexdigest()
        event_id = _stable_id(
            "contention",
            task_id or "",
            attempt or 0,
            stage,
            resource_class,
            resource_digest,
            reason,
            started,
        )
        self.store.execute(
            "INSERT INTO task_resource_contentions ("
            "id, task_id, project, attempt, stage, resource_class, resource_digest, "
            "reason, peer_task_ids, wait_started_at, wait_ended_at, wait_seconds, "
            "outcome, metadata, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET wait_ended_at = excluded.wait_ended_at, "
            "wait_seconds = excluded.wait_seconds, outcome = excluded.outcome, "
            "metadata = excluded.metadata, updated_at = excluded.updated_at",
            (
                event_id,
                task_id,
                project,
                attempt,
                stage,
                resource_class,
                resource_digest,
                reason,
                json_dumps(peers),
                started,
                ended,
                _seconds(started, ended) if ended else None,
                outcome,
                json_dumps(ensure_json_object(metadata)),
                now,
                now,
            ),
        )
        return {
            "schema": "mac.task_resource_contention.v1",
            "id": event_id,
            "task_id": task_id,
            "project": project,
            "attempt": attempt,
            "stage": stage,
            "resource_class": resource_class,
            "resource_digest": resource_digest,
            "reason": reason,
            "peer_task_ids": peers,
            "wait_started_at": started,
            "wait_ended_at": ended,
            "wait_seconds": _seconds(started, ended) if ended else None,
            "outcome": outcome,
            "metadata": ensure_json_object(metadata),
        }

    def report(
        self,
        *,
        project: Optional[str] = None,
        since_hours: float = 24.0,
        warning_seconds: float = 300.0,
        critical_seconds: float = 600.0,
        refresh_limit: int = 100,
        dispatch_explainer: Optional[Callable[[str], Mapping[str, Any]]] = None,
        idle_worker_count: Optional[int] = None,
        observed_at: Optional[str] = None,
    ) -> JsonDict:
        """Return and persist a bounded point-in-time throughput snapshot."""

        now = observed_at or utcnow()
        since = (
            parse_time(now) - timedelta(hours=max(0.01, float(since_hours)))
        ).isoformat(timespec="microseconds")
        refresh = self.refresh_stale(
            project=project,
            limit=refresh_limit,
            observed_at=now,
        )
        clauses = ["ended_at IS NOT NULL", "ended_at >= ?"]
        params: List[Any] = [since]
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        completion_rows = [
            _row_dict(row)
            for row in self.store.query_all(
                "SELECT * FROM task_completions WHERE %s ORDER BY ended_at"
                % " AND ".join(clauses),
                tuple(params),
            )
        ]
        completed = [
            row
            for row in completion_rows
            if row["outcome"] == TaskFlowOutcome.COMPLETED.value
        ]
        failed = [
            row
            for row in completion_rows
            if row["outcome"] == TaskFlowOutcome.FAILED.value
        ]
        cancelled = [
            row
            for row in completion_rows
            if row["outcome"] == TaskFlowOutcome.CANCELLED.value
        ]
        durations = [
            float(row["duration_seconds"])
            for row in completed
            if row.get("duration_seconds") is not None
        ]

        stage_clauses = ["duration_seconds IS NOT NULL", "ended_at >= ?"]
        stage_params: List[Any] = [since]
        if project is not None:
            stage_clauses.append("project = ?")
            stage_params.append(project)
        stage_rows = [
            _row_dict(row)
            for row in self.store.query_all(
                "SELECT stage, duration_seconds FROM task_flow_spans WHERE %s"
                % " AND ".join(stage_clauses),
                tuple(stage_params),
            )
        ]
        by_stage: Dict[str, List[float]] = defaultdict(list)
        for row in stage_rows:
            by_stage[str(row["stage"])].append(float(row["duration_seconds"]))
        stage_distributions = {
            stage: _distribution(values)
            for stage, values in sorted(by_stage.items())
        }

        active_clauses = [
            "s.ended_at IS NULL",
            "t.state NOT IN ('completed', 'failed', 'cancelled')",
        ]
        active_params: List[Any] = []
        if project is not None:
            active_clauses.append("s.project = ?")
            active_params.append(project)
        active_rows = [
            _row_dict(row)
            for row in self.store.query_all(
                "SELECT s.*, t.title, t.state, t.owner_agent_id, t.updated_at AS task_updated_at "
                "FROM task_flow_spans s JOIN tasks t ON t.id = s.task_id "
                "WHERE %s ORDER BY s.started_at, s.task_id LIMIT 500"
                % " AND ".join(active_clauses),
                tuple(active_params),
            )
        ]
        stranded: List[JsonDict] = []
        active_episodes: set = set()
        episode_rows: List[Tuple[Any, ...]] = []
        dispatch_explanation_count = 0
        dispatch_explanation_limit = 50
        for row in active_rows:
            age = _seconds(str(row["started_at"]), now)
            if age < float(warning_seconds):
                continue
            dispatch: Optional[Mapping[str, Any]] = None
            if (
                dispatch_explainer is not None
                and row["stage"] == TaskFlowStage.READY_QUEUE.value
                and dispatch_explanation_count < dispatch_explanation_limit
            ):
                try:
                    dispatch = dispatch_explainer(str(row["task_id"]))
                    dispatch_explanation_count += 1
                except Exception:
                    dispatch = None
            severity = "critical" if age >= float(critical_seconds) else "warning"
            reason = self._stranding_reason(str(row["stage"]), dispatch)
            fingerprint = hashlib.sha256(
                (
                    "%s\x00%s\x00%s\x00%s"
                    % (
                        row["task_id"],
                        row["attempt"],
                        row["stage"],
                        row["started_at"],
                    )
                ).encode("utf-8")
            ).hexdigest()
            episode_id = "strand_%s" % fingerprint[:32]
            active_episodes.add(episode_id)
            detail: JsonDict = {
                "schema": "mac.task_stranding_episode.v1",
                "id": episode_id,
                "task_id": row["task_id"],
                "title": row["title"],
                "project": row["project"],
                "attempt": int(row["attempt"]),
                "stage": row["stage"],
                "task_state": row["state"],
                "owner_agent_id": row["owner_agent_id"],
                "opened_at": row["started_at"],
                "last_observed_at": now,
                "age_seconds": age,
                "severity": severity,
                "reason": reason,
                "dispatch": dict(dispatch) if dispatch is not None else None,
            }
            stranded.append(detail)
            episode_rows.append(
                (
                    episode_id,
                    fingerprint,
                    row["task_id"],
                    row["project"],
                    int(row["attempt"]),
                    row["stage"],
                    row["started_at"],
                    now,
                    age,
                    severity,
                    reason,
                    json_dumps({"dispatch": detail["dispatch"]}),
                    now,
                    now,
                ),
            )
        open_episode_clauses = ["resolved_at IS NULL"]
        open_episode_params: List[Any] = []
        if project is not None:
            open_episode_clauses.append("project = ?")
            open_episode_params.append(project)
        open_episode_rows = self.store.query_all(
            "SELECT id FROM task_stranding_episodes WHERE %s"
            % " AND ".join(open_episode_clauses),
            tuple(open_episode_params),
        )
        with self.store.transaction() as conn:
            for values in episode_rows:
                conn.execute(
                    "INSERT INTO task_stranding_episodes ("
                    "id, fingerprint, task_id, project, attempt, stage, opened_at, "
                    "last_observed_at, resolved_at, age_seconds, severity, reason, "
                    "metadata, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "last_observed_at = excluded.last_observed_at, "
                    "resolved_at = NULL, age_seconds = excluded.age_seconds, "
                    "severity = excluded.severity, reason = excluded.reason, "
                    "metadata = excluded.metadata, updated_at = excluded.updated_at",
                    values,
                )
            for row in open_episode_rows:
                if str(row["id"]) not in active_episodes:
                    conn.execute(
                        "UPDATE task_stranding_episodes SET resolved_at = ?, "
                        "updated_at = ? WHERE id = ? AND resolved_at IS NULL",
                        (now, now, row["id"]),
                    )

        contention_clauses = ["created_at >= ?"]
        contention_params: List[Any] = [since]
        if project is not None:
            contention_clauses.append("project = ?")
            contention_params.append(project)
        contention_rows = [
            _row_dict(row)
            for row in self.store.query_all(
                "SELECT * FROM task_resource_contentions WHERE %s "
                "ORDER BY created_at DESC LIMIT 500"
                % " AND ".join(contention_clauses),
                tuple(contention_params),
            )
        ]
        by_resource: Dict[str, List[JsonDict]] = defaultdict(list)
        for row in contention_rows:
            by_resource[str(row["resource_class"])].append(row)
        contention_summary = {
            key: {
                "count": len(items),
                "wait": _distribution(
                    [
                        float(item["wait_seconds"])
                        for item in items
                        if item.get("wait_seconds") is not None
                    ]
                ),
                "reasons": sorted({str(item["reason"]) for item in items}),
            }
            for key, items in sorted(by_resource.items())
        }
        dispatch_round_clauses = ["completed_at >= ?"]
        dispatch_round_params: List[Any] = [since]
        if project is not None:
            # A global round may assign several projects.  Its scalar project
            # column is NULL and the bounded detail carries the exact set.
            dispatch_round_clauses.append("(project = ? OR project IS NULL)")
            dispatch_round_params.append(project)
        dispatch_round_rows = [
            _row_dict(row)
            for row in self.store.query_all(
                "SELECT * FROM dispatch_rounds WHERE %s "
                "ORDER BY completed_at DESC LIMIT 500"
                % " AND ".join(dispatch_round_clauses),
                tuple(dispatch_round_params),
            )
        ]
        if project is not None:
            scoped_rounds: List[JsonDict] = []
            for row in dispatch_round_rows:
                if row.get("project") == project:
                    scoped_rounds.append(row)
                    continue
                detail = json_loads(row.get("detail"), {})
                projects = {
                    str(value)
                    for value in (detail.get("projects") or [])
                    if str(value)
                }
                if project in projects:
                    scoped_rounds.append(row)
            dispatch_round_rows = scoped_rounds
        mismatch_sql = "SELECT * FROM dispatch_mismatch_state"
        mismatch_params: List[Any] = []
        if project is not None:
            mismatch_sql += " WHERE scope = ?"
            mismatch_params.append(project)
        mismatch_sql += " ORDER BY opened_at DESC"
        mismatch_rows = [
            _row_dict(row)
            for row in self.store.query_all(mismatch_sql, tuple(mismatch_params))
        ]
        active_dispatch_mismatches = [
            row for row in mismatch_rows if row.get("resolved_at") is None
        ]
        window_hours = max(0.01, float(since_hours))
        report: JsonDict = {
            "schema": "mac.task_flow_snapshot.v1",
            "observed_at": now,
            "project": project,
            "window": {"since": since, "hours": window_hours},
            "slo": {
                "basic_cycle_target_p50_seconds": 300,
                "basic_cycle_target_p95_seconds": 600,
                "ready_to_claim_target_p50_seconds": 30,
                "ready_to_claim_target_p95_seconds": 120,
                "stranding_warning_seconds": float(warning_seconds),
                "stranding_critical_seconds": float(critical_seconds),
            },
            "throughput": {
                "completed_count": len(completed),
                "failed_attempt_count": len(failed),
                "cancelled_attempt_count": len(cancelled),
                "completed_per_hour": len(completed) / window_hours,
                "end_to_end": _distribution(durations),
                "completed_under_5_minutes": sum(1 for value in durations if value <= 300),
                "completed_under_10_minutes": sum(1 for value in durations if value <= 600),
            },
            "stages": stage_distributions,
            "active": {
                "count": len(active_rows),
                "idle_worker_count": idle_worker_count,
                "age": _distribution(
                    [_seconds(str(row["started_at"]), now) for row in active_rows]
                ),
            },
            "stranding": {
                "count": len(stranded),
                "critical_count": sum(
                    1 for item in stranded if item["severity"] == "critical"
                ),
                "dispatch_explanation_count": dispatch_explanation_count,
                "dispatch_explanation_limit": dispatch_explanation_limit,
                "episodes": stranded,
            },
            "contention": {
                "count": len(contention_rows),
                "by_resource_class": contention_summary,
                "recent": [
                    {
                        **row,
                        "peer_task_ids": json_loads(row.get("peer_task_ids"), []),
                        "metadata": json_loads(row.get("metadata"), {}),
                    }
                    for row in contention_rows[:100]
                ],
            },
            "dispatch": {
                "round_count": len(dispatch_round_rows),
                "assignment_count": sum(
                    int(row["assignment_count"]) for row in dispatch_round_rows
                ),
                "claim_failure_count": sum(
                    int(row["claim_failure_count"]) for row in dispatch_round_rows
                ),
                "false_ready_count": sum(
                    int(row["false_ready_count"]) for row in dispatch_round_rows
                ),
                "round_duration": _distribution(
                    [
                        float(row["duration_seconds"])
                        for row in dispatch_round_rows
                    ]
                ),
                "ready_free_capacity_mismatch": {
                    "active_count": len(active_dispatch_mismatches),
                    "warning_count": sum(
                        1
                        for row in active_dispatch_mismatches
                        if row["severity"] == "warning"
                    ),
                    "critical_count": sum(
                        1
                        for row in active_dispatch_mismatches
                        if row["severity"] == "critical"
                    ),
                    "episodes": [
                        {
                            **row,
                            "metadata": json_loads(row.get("metadata"), {}),
                        }
                        for row in active_dispatch_mismatches
                    ],
                },
                "recent_rounds": [
                    {
                        **row,
                        "detail": json_loads(row.get("detail"), {}),
                    }
                    for row in dispatch_round_rows[:100]
                ],
            },
            "materialization": refresh,
            "data_sources": [
                "tasks",
                "task_history",
                "leases",
                "reviews",
                "publications",
                "task_flow_spans",
                "task_completions",
                "task_stranding_episodes",
                "task_resource_contentions",
                "dispatch_rounds",
                "dispatch_mismatch_state",
            ],
        }
        snapshot_id = _stable_id("flowsnap", project or "*", now[:13])
        report["snapshot_id"] = snapshot_id
        retention_cutoff = (
            parse_time(now) - timedelta(days=90)
        ).isoformat(timespec="microseconds")
        self.store.execute(
            "INSERT INTO task_flow_snapshots ("
            "id, project, observed_at, since_at, warning_seconds, critical_seconds, "
            "report, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET observed_at = excluded.observed_at, "
            "since_at = excluded.since_at, warning_seconds = excluded.warning_seconds, "
            "critical_seconds = excluded.critical_seconds, report = excluded.report",
            (
                snapshot_id,
                project,
                now,
                since,
                float(warning_seconds),
                float(critical_seconds),
                json_dumps(report),
                now,
            ),
        )
        self.store.execute(
            "DELETE FROM task_flow_snapshots WHERE created_at < ?",
            (retention_cutoff,),
        )
        return report
