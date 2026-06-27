"""Canonical MAC action event ledger.

The action ledger is the normalized operational stream for commands,
OpenShell events, task/session lifecycle, policy decisions, and summarized
memory writebacks. Older surfaces such as ``command_audit`` and
``observability_events`` remain readable, but new writes can also project here
as ``mac.action_event.v1`` records.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from mac.models import (
    ACTION_EVENT_OUTCOMES,
    OBSERVABILITY_LEVELS,
    ActionEvent,
    JsonDict,
    ObservabilityEvent,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
)


ACTION_EVENT_SCHEMA = "mac.action_event.v1"

# retention-prereq: cap the serialised attributes JSON on action events so
# they don't bypass the observability detail-size cap and bloat the table.
# Action events project observability detail verbatim into the attributes
# column, which previously had no size guard.  The cap mirrors
# ObservabilityService.MAX_DETAIL_BYTES so both tables stay bounded.
ACTION_EVENT_MAX_ATTRIBUTES_BYTES = 64 * 1024


def _clean_optional(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_severity(value: str) -> str:
    severity = str(value or "info").strip().lower()
    if severity == "warn":
        severity = "warning"
    if severity not in OBSERVABILITY_LEVELS:
        raise ValidationError("unsupported action event severity: %s" % value)
    return severity


def _normalize_outcome(value: str) -> str:
    outcome = str(value or "unknown").strip().lower()
    if outcome not in ACTION_EVENT_OUTCOMES:
        raise ValidationError(
            "unsupported action event outcome: %s (allowed: %s)"
            % (value, ", ".join(sorted(ACTION_EVENT_OUTCOMES)))
        )
    return outcome


def _stable_hex(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _cap_attributes(attrs: dict) -> dict:
    """Cap serialised attributes to ACTION_EVENT_MAX_ATTRIBUTES_BYTES.

    If the JSON representation exceeds the cap, replace the payload with a
    truncation marker that preserves the top-level keys so operators can still
    diagnose which emitter produced the oversized event.
    """
    serialised = json_dumps(attrs)
    if len(serialised.encode("utf-8")) <= ACTION_EVENT_MAX_ATTRIBUTES_BYTES:
        return attrs
    return {
        "_truncated": True,
        "_max_bytes": ACTION_EVENT_MAX_ATTRIBUTES_BYTES,
        "_original_bytes": len(serialised.encode("utf-8")),
        "_keys": list(attrs.keys())[:50],
    }


class ActionEventService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def record_action_event(
        self,
        *,
        event_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        agent_id: Optional[str] = None,
        hermes_instance_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
        actor: str = "mac",
        action_type: str,
        action_name: str,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        outcome: str = "unknown",
        severity: str = "info",
        policy_id: Optional[str] = None,
        policy_version: Optional[int] = None,
        command_id: Optional[str] = None,
        parent_event_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
        redaction_state: str = "redacted",
        conn: Any = None,
    ) -> ActionEvent:
        writer = conn if conn is not None else self.store
        event = self._coerce_event(
            {
                "event_id": event_id or new_id("act"),
                "timestamp": timestamp or utcnow(),
                "agent_id": agent_id,
                "hermes_instance_id": hermes_instance_id,
                "task_id": task_id,
                "session_id": session_id,
                "sandbox_id": sandbox_id,
                "actor": actor,
                "action_type": action_type,
                "action_name": action_name,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "outcome": outcome,
                "severity": severity,
                "policy_id": policy_id,
                "policy_version": policy_version,
                "command_id": command_id,
                "parent_event_id": parent_event_id,
                "attributes": attributes or {},
                "redaction_state": redaction_state,
            }
        )
        writer.execute(
            """
            INSERT INTO action_events (
                event_id, timestamp, agent_id, hermes_instance_id, task_id,
                session_id, sandbox_id, actor, action_type, action_name,
                subject_type, subject_id, outcome, severity, policy_id,
                policy_version, command_id, parent_event_id, attributes,
                redaction_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.timestamp,
                event.agent_id,
                event.hermes_instance_id,
                event.task_id,
                event.session_id,
                event.sandbox_id,
                event.actor,
                event.action_type,
                event.action_name,
                event.subject_type,
                event.subject_id,
                event.outcome,
                event.severity,
                event.policy_id,
                event.policy_version,
                event.command_id,
                event.parent_event_id,
                json_dumps(event.attributes),
                event.redaction_state,
            ),
        )
        return event

    def project_observability(
        self,
        conn: Any,
        event: ObservabilityEvent,
    ) -> Optional[ActionEvent]:
        detail = ensure_json_object(event.detail)
        actor = str(event.source or "mac")
        outcome = "success"
        if event.level in {"error", "critical"}:
            outcome = "failure"
        return self.record_action_event(
            event_id="act_obs_%s" % event.id,
            timestamp=event.created_at,
            agent_id=event.source if event.layer in {"worker", "executor"} else None,
            task_id=event.subject_id if event.subject_type == "task" else None,
            actor=actor,
            action_type="observability",
            action_name=event.name,
            subject_type=event.subject_type,
            subject_id=event.subject_id,
            outcome=outcome,
            severity=event.level,
            attributes={
                "schema": ACTION_EVENT_SCHEMA,
                "observability_id": event.id,
                "observability_sequence": event.sequence,
                "kind": event.kind,
                "layer": event.layer,
                "source": event.source,
                "value": event.value,
                "unit": event.unit,
                "detail": detail,
            },
            redaction_state="redacted",
            conn=conn,
        )

    def project_command_audit(
        self,
        conn: Any,
        *,
        audit_id: str,
        command_id: str,
        agent_id: str,
        phase: str,
        argv: Iterable[str],
        cwd: str,
        task_id: Optional[str],
        lease_id: Optional[str],
        started_at: Optional[str],
        completed_at: Optional[str],
        duration_ms: Optional[float],
        returncode: Optional[int],
        stdout_sha256: Optional[str],
        stderr_sha256: Optional[str],
        stdout_bytes: Optional[int],
        stderr_bytes: Optional[int],
        metadata: Optional[Dict[str, Any]],
        timestamp: str,
    ) -> ActionEvent:
        phase_value = str(phase or "").strip().lower()
        outcome = {
            "started": "started",
            "completed": "success",
            "failed": "failure",
            "timeout": "failure",
            "error": "failure",
        }.get(phase_value, "unknown")
        argv_list = [str(item) for item in argv]
        action_name = argv_list[0] if argv_list else "command"
        return self.record_action_event(
            event_id="act_cmd_%s" % audit_id,
            timestamp=timestamp,
            agent_id=agent_id,
            task_id=task_id,
            actor=agent_id,
            action_type="command",
            action_name=action_name,
            subject_type="task" if task_id else "agent",
            subject_id=task_id or agent_id,
            outcome=outcome,
            severity="error" if outcome == "failure" else "info",
            command_id=command_id,
            attributes={
                "schema": ACTION_EVENT_SCHEMA,
                "audit_id": audit_id,
                "phase": phase_value,
                "argv": argv_list,
                "argv_redacted": True,
                "cwd": cwd,
                "lease_id": lease_id,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": duration_ms,
                "returncode": returncode,
                "stdout_sha256": stdout_sha256,
                "stderr_sha256": stderr_sha256,
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": stderr_bytes,
                "metadata": ensure_json_object(metadata),
            },
            redaction_state="redacted",
            conn=conn,
        )

    def list_action_events(
        self,
        *,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
        policy_id: Optional[str] = None,
        action_type: Optional[str] = None,
        outcome: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
    ) -> List[ActionEvent]:
        clauses: List[str] = []
        params: List[Any] = []
        filters = {
            "agent_id": agent_id,
            "task_id": task_id,
            "session_id": session_id,
            "sandbox_id": sandbox_id,
            "policy_id": policy_id,
            "action_type": action_type,
            "outcome": outcome,
        }
        for column, value in filters.items():
            if value is not None:
                clauses.append("%s = ?" % column)
                params.append(str(value))
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            clauses.append("timestamp <= ?")
            params.append(until)
        sql = "SELECT * FROM action_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp DESC, event_id DESC LIMIT ?"
        params.append(min(max(1, int(limit)), 1000))
        return [self._from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def export_otlp(
        self,
        *,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        sandbox_id: Optional[str] = None,
        policy_id: Optional[str] = None,
        action_type: Optional[str] = None,
        outcome: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 1000,
    ) -> JsonDict:
        events = self.list_action_events(
            agent_id=agent_id,
            task_id=task_id,
            session_id=session_id,
            sandbox_id=sandbox_id,
            policy_id=policy_id,
            action_type=action_type,
            outcome=outcome,
            since=since,
            until=until,
            limit=limit,
        )
        spans = []
        logs = []
        counts: Counter[str] = Counter()
        for event in reversed(events):
            ns = self._unix_nano(event.timestamp)
            trace_basis = event.session_id or event.task_id or event.sandbox_id or event.agent_id or event.event_id
            span = {
                "traceId": _stable_hex(trace_basis, 32),
                "spanId": _stable_hex(event.event_id, 16),
                "parentSpanId": _stable_hex(event.parent_event_id, 16) if event.parent_event_id else "",
                "name": "%s.%s" % (event.action_type, event.action_name),
                "startTimeUnixNano": ns,
                "endTimeUnixNano": ns,
                "attributes": self._otel_attributes(event),
                "status": {"code": "ERROR" if event.outcome in {"failure", "denied"} else "OK"},
            }
            spans.append(span)
            logs.append(
                {
                    "timeUnixNano": ns,
                    "severityText": event.severity.upper(),
                    "body": {
                        "stringValue": "%s.%s %s"
                        % (event.action_type, event.action_name, event.outcome)
                    },
                    "attributes": self._otel_attributes(event),
                }
            )
            counts["action_type:%s" % event.action_type] += 1
            counts["outcome:%s" % event.outcome] += 1
            counts["severity:%s" % event.severity] += 1
        metrics = [
            {"name": key, "value": value, "unit": "1"}
            for key, value in sorted(counts.items())
        ]
        return {
            "schema": "mac.action_events.otlp_export.v1",
            "resourceSpans": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": "mac"}]},
                    "scopeSpans": [{"scope": {"name": "mac.action_event.v1"}, "spans": spans}],
                }
            ],
            "resourceLogs": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": "mac"}]},
                    "scopeLogs": [{"scope": {"name": "mac.action_event.v1"}, "logRecords": logs}],
                }
            ],
            "metrics": metrics,
            "event_count": len(events),
        }

    def summarize(
        self,
        *,
        agent_id: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 1000,
    ) -> JsonDict:
        events = self.list_action_events(agent_id=agent_id, since=since, limit=limit)
        by_type = Counter(event.action_type for event in events)
        by_outcome = Counter(event.outcome for event in events)
        denials = [event for event in events if event.outcome == "denied"]
        failures = [event for event in events if event.outcome == "failure"]
        return {
            "schema": "mac.action_summary.v1",
            "agent_id": agent_id,
            "since": since,
            "event_count": len(events),
            "by_action_type": dict(sorted(by_type.items())),
            "by_outcome": dict(sorted(by_outcome.items())),
            "denial_count": len(denials),
            "failure_count": len(failures),
            "notable_events": [
                {
                    "event_id": event.event_id,
                    "timestamp": event.timestamp,
                    "action_type": event.action_type,
                    "action_name": event.action_name,
                    "outcome": event.outcome,
                    "subject_type": event.subject_type,
                    "subject_id": event.subject_id,
                }
                for event in [*denials[:5], *failures[:5]][:10]
            ],
        }

    def _coerce_event(self, raw: Dict[str, Any]) -> ActionEvent:
        action_type = str(raw.get("action_type") or "").strip()
        action_name = str(raw.get("action_name") or "").strip()
        actor = str(raw.get("actor") or "").strip()
        if not action_type or not action_name or not actor:
            raise ValidationError("action event requires actor, action_type, and action_name")
        policy_version = raw.get("policy_version")
        return ActionEvent(
            event_id=str(raw.get("event_id") or new_id("act")),
            timestamp=str(raw.get("timestamp") or utcnow()),
            agent_id=_clean_optional(raw.get("agent_id")),
            hermes_instance_id=_clean_optional(raw.get("hermes_instance_id")),
            task_id=_clean_optional(raw.get("task_id")),
            session_id=_clean_optional(raw.get("session_id")),
            sandbox_id=_clean_optional(raw.get("sandbox_id")),
            actor=actor,
            action_type=action_type,
            action_name=action_name,
            subject_type=_clean_optional(raw.get("subject_type")),
            subject_id=_clean_optional(raw.get("subject_id")),
            outcome=_normalize_outcome(str(raw.get("outcome") or "unknown")),
            severity=_normalize_severity(str(raw.get("severity") or "info")),
            policy_id=_clean_optional(raw.get("policy_id")),
            policy_version=int(policy_version) if policy_version is not None else None,
            command_id=_clean_optional(raw.get("command_id")),
            parent_event_id=_clean_optional(raw.get("parent_event_id")),
            attributes=_cap_attributes(ensure_json_object(raw.get("attributes") or {})),
            redaction_state=str(raw.get("redaction_state") or "redacted"),
        )

    def _from_row(self, row: Any) -> ActionEvent:
        return ActionEvent(
            event_id=row["event_id"],
            timestamp=row["timestamp"],
            agent_id=row["agent_id"],
            hermes_instance_id=row["hermes_instance_id"],
            task_id=row["task_id"],
            session_id=row["session_id"],
            sandbox_id=row["sandbox_id"],
            actor=row["actor"],
            action_type=row["action_type"],
            action_name=row["action_name"],
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            outcome=row["outcome"],
            severity=row["severity"],
            policy_id=row["policy_id"],
            policy_version=row["policy_version"],
            command_id=row["command_id"],
            parent_event_id=row["parent_event_id"],
            attributes=json_loads(row["attributes"], {}),
            redaction_state=row["redaction_state"],
        )

    def _otel_attributes(self, event: ActionEvent) -> List[JsonDict]:
        attrs = {
            "event_id": event.event_id,
            "agent_id": event.agent_id,
            "hermes_instance_id": event.hermes_instance_id,
            "task_id": event.task_id,
            "session_id": event.session_id,
            "sandbox_id": event.sandbox_id,
            "actor": event.actor,
            "action_type": event.action_type,
            "action_name": event.action_name,
            "subject_type": event.subject_type,
            "subject_id": event.subject_id,
            "outcome": event.outcome,
            "severity": event.severity,
            "policy_id": event.policy_id,
            "policy_version": event.policy_version,
            "command_id": event.command_id,
            "redaction_state": event.redaction_state,
        }
        return [
            {"key": key, "value": value}
            for key, value in attrs.items()
            if value is not None
        ]

    def _unix_nano(self, value: str) -> int:
        try:
            dt = parse_time(value)
        except Exception:
            dt = datetime.fromisoformat(utcnow())
        return int(dt.timestamp() * 1_000_000_000)
