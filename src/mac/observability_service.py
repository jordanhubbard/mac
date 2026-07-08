"""Observability domain service.

Owns the ``observability_events`` table: metric/log writes, queries, and
retention. ``ControlPlane`` holds an instance of this service and delegates;
internal call sites that need to record an observation as part of a larger
transaction call ``insert_observation(conn, ...)`` with their open
connection, so the observation row commits or rolls back with the rest of
the transaction.

This is the first domain to be extracted from the historical god-class. New
domains should follow the same shape: take ``store`` in ``__init__``, expose
a focused public API, accept an optional ``conn`` on writes so callers can
participate in cross-domain transactions.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Callable, Dict, List, Optional

from mac.models import (
    OBSERVABILITY_KINDS,
    OBSERVABILITY_LEVELS,
    JsonDict,
    ObservabilityEvent,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)

OBSERVABILITY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,127}$")

# mem-04: the original audit on rocky 2026-05-28 found 1.83M of 2.09M
# total observability_events rows came from these six emitters, all
# of which fire on every poll regardless of state change. They're
# debug-level by design but the table still grew to 3.1GB. By default
# we drop them at the record_log boundary; operators flip
# MAC_OBSERVABILITY_VERBOSE_POLL=1 to re-enable for debugging.
_VERBOSE_POLL_LOG_NAMES = frozenset(
    {
        "worker.routing.no_candidate",
        "worker.no_task",
        # Per-candidate reasons are useful during a bounded routing debug run,
        # but a held fleet emits up to 25 of each on every two-second poll.  In
        # one live incident these two names produced 7.5M rows and a 15.9GB
        # control-plane database.  Keep them behind the existing verbose-poll
        # switch; durable hold state and the aggregate held status remain visible.
        "worker.routing.task_skipped",
        "dispatcher.routing.task_skipped",
        "workflow.default_review.waiting",
        "workflow.default_review.waiting_for_verdict",
        "workflow.default_review.heartbeat_tick",
        "workflow.default_review.heartbeat_tick_failed",
        # Re-emitted every review tick for a task stuck waiting on an operator to
        # set metadata.publication_target — a steady-state condition, not an
        # event. It alone wrote 262K rows in ~4 days on rocky.
        "workflow.default_review.no_publication_target",
    }
)


def _verbose_poll_enabled() -> bool:
    return os.environ.get("MAC_OBSERVABILITY_VERBOSE_POLL", "0") not in {"", "0", "false", "False"}


class ObservabilityService:
    # mac-29vr: cap the serialised detail JSON we will persist on a
    # single observation. The events table fans out across every layer
    # and a chatty caller (e.g., a worker that logs full subprocess
    # output) can otherwise pump GB into one SQLite table.
    MAX_DETAIL_BYTES = 64 * 1024

    def __init__(
        self,
        store: Any,
        action_event_recorder: Optional[Callable[[Any, ObservabilityEvent], Any]] = None,
    ) -> None:
        self.store = store
        self._action_event_recorder = action_event_recorder

    # Public API ---------------------------------------------------------

    def record_observation(
        self,
        kind: str,
        name: str,
        layer: str = "control_plane",
        source: str = "mac",
        level: str = "info",
        value: Optional[float] = None,
        unit: str = "",
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> ObservabilityEvent:
        return self.insert_observation(
            self.store,
            kind,
            name,
            layer,
            source,
            level,
            value,
            unit,
            subject_type,
            subject_id,
            detail or {},
            utcnow(),
        )

    def _record_log_unfiltered(
        self,
        name: str,
        level: str = "info",
        layer: str = "control_plane",
        source: str = "mac",
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> ObservabilityEvent:
        """Internal record_log that bypasses the mem-04 verbose-poll
        suppression. Use this for tests and for emitters that have
        genuinely changing state per call."""
        return self.record_observation(
            "log", name, layer, source, level, None, "",
            subject_type, subject_id, detail,
        )

    def record_metric(
        self,
        name: str,
        value: float,
        unit: str = "",
        layer: str = "control_plane",
        source: str = "mac",
        level: str = "info",
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> ObservabilityEvent:
        return self.record_observation(
            "metric",
            name,
            layer,
            source,
            level,
            value,
            unit,
            subject_type,
            subject_id,
            detail,
        )

    def record_log(
        self,
        name: str,
        level: str = "info",
        layer: str = "control_plane",
        source: str = "mac",
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Optional[ObservabilityEvent]:
        # mem-04: silence the high-volume idle-poll log names by
        # default. The original audit found that 1.83M of 2.09M total
        # observability rows on rocky came from these six emitters,
        # all of which fire on every poll regardless of state change.
        # Operators who need them for debugging can re-enable with
        # MAC_OBSERVABILITY_VERBOSE_POLL=1 in the mac.env file.
        if name in _VERBOSE_POLL_LOG_NAMES and not _verbose_poll_enabled():
            return None
        return self.record_observation(
            "log",
            name,
            layer,
            source,
            level,
            None,
            "",
            subject_type,
            subject_id,
            detail,
        )

    def list_observability(
        self,
        kind: Optional[str] = None,
        layer: Optional[str] = None,
        level: Optional[str] = None,
        name: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        after_sequence: Optional[int] = None,
        limit: int = 100,
    ) -> List[ObservabilityEvent]:
        clauses: List[str] = []
        params: List[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(self.normalize_kind(kind))
        if layer is not None:
            clauses.append("layer = ?")
            params.append(self.validate_name(layer, "layer"))
        if level is not None:
            clauses.append("level = ?")
            params.append(self.normalize_level(level))
        if name is not None:
            clauses.append("name = ?")
            params.append(self.validate_name(name, "name"))
        if subject_type is not None:
            clauses.append("subject_type = ?")
            params.append(subject_type)
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        if after_sequence is not None:
            clauses.append("sequence > ?")
            params.append(max(0, int(after_sequence)))
        sql = "SELECT * FROM observability_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if after_sequence is not None:
            sql += " ORDER BY sequence ASC LIMIT ?"
        else:
            sql += " ORDER BY sequence DESC LIMIT ?"
        params.append(min(max(1, int(limit)), 1000))
        return [
            self._from_row(row)
            for row in self.store.query_all(sql, tuple(params))
        ]

    def prune(
        self,
        older_than: Optional[str] = None,
        keep_last: Optional[int] = None,
    ) -> int:
        """Delete observability rows older than ``older_than`` (ISO timestamp)
        or keep only the most recent ``keep_last`` rows. Returns the number of
        rows removed."""
        if older_than is None and keep_last is None:
            raise ValidationError("prune_observability requires older_than or keep_last")
        with self.store.transaction() as conn:
            removed = 0
            if older_than is not None:
                cursor = conn.execute(
                    "DELETE FROM observability_events WHERE created_at < ?",
                    (older_than,),
                )
                removed += int(cursor.rowcount or 0)
            if keep_last is not None:
                kept = max(0, int(keep_last))
                cursor = conn.execute(
                    """
                    DELETE FROM observability_events
                    WHERE sequence <= COALESCE(
                        (SELECT sequence FROM observability_events
                         ORDER BY sequence DESC LIMIT 1 OFFSET ?), 0
                    )
                    """,
                    (kept,),
                )
                removed += int(cursor.rowcount or 0)
        return removed

    def summary(self, limit: int = 80) -> JsonDict:
        latest = self.list_observability(limit=limit)
        levels: Dict[str, int] = {}
        layers: Dict[str, int] = {}
        for item in latest:
            levels[item.level] = levels.get(item.level, 0) + 1
            layers[item.layer] = layers.get(item.layer, 0) + 1
        metric_rows = self.store.query_all(
            """
            SELECT * FROM observability_events
            WHERE kind = 'metric'
            ORDER BY sequence DESC
            LIMIT 500
            """
        )
        seen = set()
        latest_metrics: List[JsonDict] = []
        for row in metric_rows:
            item = self._from_row(row)
            key = (item.layer, item.source, item.name, item.unit)
            if key in seen:
                continue
            seen.add(key)
            latest_metrics.append(item.to_dict())
            if len(latest_metrics) >= 24:
                break
        counts = {
            "events": self._count(),
            "metrics": self._count(kind="metric"),
            "logs": self._count(kind="log"),
            "warnings": self._count(level="warning"),
            "errors": self._count(level="error") + self._count(level="critical"),
        }
        return {
            "counts": counts,
            "levels": levels,
            "layers": layers,
            "latest": [item.to_dict() for item in latest],
            "latest_metrics": latest_metrics,
        }

    # Transactional insertion -------------------------------------------

    def insert_observation(
        self,
        conn: Any,
        kind: str,
        name: str,
        layer: str,
        source: str,
        level: str,
        value: Optional[float],
        unit: str,
        subject_type: Optional[str],
        subject_id: Optional[str],
        detail: Dict[str, Any],
        when: str,
    ) -> ObservabilityEvent:
        kind_value = self.normalize_kind(kind)
        level_value = self.normalize_level(level)
        layer_value = self.validate_name(layer or "control_plane", "layer")
        source_value = self.validate_name(source or "mac", "source")
        name_value = self.validate_name(name, "name")
        value_float = self._normalize_value(kind_value, value)
        obs_id = new_id("obs")
        unit_value = str(unit or "")
        detail_json = json_dumps(ensure_json_object(detail))
        # mac-29vr: cap detail size. If we'd exceed MAX_DETAIL_BYTES,
        # replace it with a truncated marker so we keep the audit record
        # but don't bloat the table.
        if len(detail_json.encode("utf-8")) > self.MAX_DETAIL_BYTES:
            detail_json = json_dumps(
                {
                    "_truncated": True,
                    "_max_bytes": self.MAX_DETAIL_BYTES,
                    "_original_bytes": len(detail_json.encode("utf-8")),
                    "_keys": list(ensure_json_object(detail).keys())[:50],
                }
            )
        # RETURNING is supported by both SQLite (>=3.35, 2021) and Postgres.
        # Replaces an earlier `cursor.lastrowid` read that worked under
        # SQLite but broke on Postgres because the PostgresStore _Result
        # adapter does not expose lastrowid (Postgres has no implicit
        # last-row-id concept — the sequence value is only available via
        # RETURNING or LASTVAL(), and RETURNING is portable).
        result = conn.execute(
            """
            INSERT INTO observability_events (
                id, kind, layer, source, level, name, subject_type, subject_id,
                value, unit, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING sequence
            """,
            (
                obs_id,
                kind_value,
                layer_value,
                source_value,
                level_value,
                name_value,
                subject_type,
                subject_id,
                value_float,
                unit_value,
                detail_json,
                when,
            ),
        )
        sequence_row = result.fetchone()
        if sequence_row is None:
            raise RuntimeError("INSERT ... RETURNING sequence yielded no row")
        sequence_value = sequence_row[0] if not hasattr(sequence_row, "keys") else sequence_row["sequence"]
        event = ObservabilityEvent(
            int(sequence_value),
            obs_id,
            kind_value,
            layer_value,
            source_value,
            level_value,
            name_value,
            subject_type,
            subject_id,
            value_float,
            unit_value,
            json_loads(detail_json, {}),
            when,
        )
        if self._action_event_recorder is not None:
            self._action_event_recorder(conn, event)
        return event

    # Validation helpers -------------------------------------------------

    def normalize_kind(self, kind: str) -> str:
        value = str(kind or "").strip().lower()
        if value not in OBSERVABILITY_KINDS:
            raise ValidationError(
                "unsupported observability kind: %s (allowed: %s)"
                % (kind, ", ".join(sorted(OBSERVABILITY_KINDS)))
            )
        return value

    def normalize_level(self, level: str) -> str:
        value = str(level or "info").strip().lower()
        if value == "warn":
            value = "warning"
        if value not in OBSERVABILITY_LEVELS:
            raise ValidationError(
                "unsupported observability level: %s (allowed: %s)"
                % (level, ", ".join(sorted(OBSERVABILITY_LEVELS)))
            )
        return value

    def validate_name(self, value: str, field: str) -> str:
        text = str(value or "").strip()
        if not OBSERVABILITY_NAME_RE.match(text):
            raise ValidationError("invalid observability %s: %s" % (field, value))
        return text

    def _normalize_value(self, kind: str, value: Optional[float]) -> Optional[float]:
        if value is None:
            if kind == "metric":
                raise ValidationError("metric observations require a numeric value")
            return None
        number = float(value)
        if not math.isfinite(number):
            raise ValidationError("observability value must be finite")
        return number

    # Internal -----------------------------------------------------------

    def _from_row(self, row: Any) -> ObservabilityEvent:
        return ObservabilityEvent(
            int(row["sequence"]),
            row["id"],
            row["kind"],
            row["layer"],
            row["source"],
            row["level"],
            row["name"],
            row["subject_type"],
            row["subject_id"],
            row["value"],
            row["unit"],
            json_loads(row["detail"], {}),
            row["created_at"],
        )

    def _count(self, kind: Optional[str] = None, level: Optional[str] = None) -> int:
        clauses = []
        params: List[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if level is not None:
            clauses.append("level = ?")
            params.append(level)
        sql = "SELECT COUNT(*) AS count FROM observability_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        row = self.store.query_one(sql, tuple(params))
        return int(row["count"]) if row is not None else 0
