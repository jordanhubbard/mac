"""Durable supervisor-observed crash evidence and repair dispatch.

The crashing worker is deliberately not responsible for this protocol. A
small parent observer posts (or locally spools) one occurrence after the child
exits. The hub computes the fingerprint so a compromised or outdated worker
cannot choose the deduplication key, retains every occurrence, and creates one
repair task per active revision+stack incident.
"""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Any, Dict, List, Optional

from mac.models import (
    JsonDict,
    NotFoundError,
    TaskState,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    parse_time,
    utcnow,
)
from mac.generator_yield import GeneratorSuppressed

CRASH_REPORT_SCHEMA = "mac.agent_crash_report.v1"
CRASH_OCCURRENCE_SCHEMA = "mac.agent_crash_occurrence.v1"
MAX_TRACE_CHARS = 64 * 1024
MAX_TEXT_CHARS = 64 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_REPAIR_ATTEMPTS = 3
SUPPORTED_SUPERVISORS = {"systemd", "launchd", "supervisord", "kubernetes", "manual"}
_ACTIVE_TASK_STATES = {
    TaskState.OPEN.value,
    TaskState.WAITING.value,
    TaskState.BLOCKED.value,
    TaskState.CLAIMED.value,
    TaskState.RUNNING.value,
    TaskState.NEEDS_REVIEW.value,
    TaskState.REVIEWING.value,
}

_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]+")
_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+(?:Z)?\b")
_PID_RE = re.compile(r"\b(?:pid|process)\s*[=: ]\s*\d+\b", re.IGNORECASE)
_HOME_RE = re.compile(r"/(?:Users|home)/[^/]+/")
_SPACE_RE = re.compile(r"\s+")
_SECRET_RE = re.compile(
    r"(?i)(\b(?:authorization|bearer|token|password|secret|api[_-]?key)\b\s*[:=]?\s*)([^\s,;]+)"
)
_URL_AUTH_RE = re.compile(r"(https?://)([^/@\s]+)@", re.IGNORECASE)
_KNOWN_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|sk-[A-Za-z0-9_-]{12,})\b")
_SECRET_KEY_RE = re.compile(r"(?i)(token|secret|password|credential|api[_-]?key|private[_-]?key)")


def _redact_text(value: Any) -> str:
    text = str(value or "").replace("\x00", "")
    text = _URL_AUTH_RE.sub(r"\1<redacted>@", text)
    text = _SECRET_RE.sub(r"\1<redacted>", text)
    return _KNOWN_TOKEN_RE.sub("<redacted>", text)


def _redact_json(value: Any, key: str = "") -> Any:
    if _SECRET_KEY_RE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact_json(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _bounded_text(value: Any, name: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = _redact_text(value)
    if len(text) > limit:
        # Preserve the end: Python exception classes and native fatal summaries
        # are normally emitted after their frames.
        text = text[-limit:]
    if len(text) > limit:
        raise ValidationError("%s exceeds %d-character limit" % (name, limit))
    return text


def _bounded_json(value: Any, name: str) -> JsonDict:
    obj = ensure_json_object(_redact_json(value))
    encoded = json_dumps(obj)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValidationError("%s exceeds %d-byte limit" % (name, MAX_METADATA_BYTES))
    return obj


def normalize_stack_trace(stack_trace: str, stderr_tail: str, reason: str) -> str:
    """Return a stable, secret-minimizing signature source.

    Revision is hashed separately. Volatile addresses, timestamps, user-home
    prefixes, and process identifiers are removed while retaining exception
    types, source paths, functions, and native frame names.
    """
    source = stack_trace.strip() or stderr_tail.strip() or reason.strip() or "unknown crash"
    lines: List[str] = []
    for raw in source.splitlines()[-160:]:
        line = _ADDRESS_RE.sub("0xADDR", raw.strip())
        line = _TIMESTAMP_RE.sub("<TIME>", line)
        line = _PID_RE.sub("pid=<PID>", line)
        line = _HOME_RE.sub("/<HOME>/", line)
        line = _SPACE_RE.sub(" ", line).strip()
        if line and line not in lines[-3:]:
            lines.append(line[:1000])
    return "\n".join(lines[-80:])[:MAX_TRACE_CHARS]


def crash_fingerprint(
    *, revision: str, process_name: str, stack_signature: str, exit_code: Any, signal: Any
) -> str:
    identity = {
        "schema": CRASH_REPORT_SCHEMA,
        "revision": revision or "unknown",
        "process_name": process_name or "mac-agent",
        "termination": "signal:%s" % signal if signal is not None else "exit:%s" % exit_code,
        "stack_signature": stack_signature,
    }
    return "sha256:" + hashlib.sha256(json_dumps(identity).encode("utf-8")).hexdigest()


class CrashService:
    def __init__(self, control_plane: Any) -> None:
        self.cp = control_plane
        self.store = control_plane.store
        self._lock = threading.RLock()

    def ingest(self, agent_id: str, payload: Dict[str, Any]) -> JsonDict:
        self.cp.get_agent(agent_id)
        data = ensure_json_object(payload)
        event_id = _bounded_text(data.get("event_id"), "event_id", 160).strip()
        if not event_id:
            raise ValidationError("crash event_id is required")
        process_name = _bounded_text(data.get("process_name") or "mac-agent", "process_name", 160)
        revision = _bounded_text(data.get("revision") or "unknown", "revision", 160)
        observed_raw = _bounded_text(data.get("observed_at") or utcnow(), "observed_at", 80)
        try:
            observed_at = parse_time(observed_raw).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValidationError("crash observed_at must be an ISO-8601 timestamp") from exc
        supervisor = _bounded_text(data.get("supervisor") or "unknown", "supervisor", 80)
        if supervisor not in SUPPORTED_SUPERVISORS:
            raise ValidationError("unsupported crash supervisor: %s" % supervisor)
        reason = _bounded_text(data.get("reason") or "process exited unexpectedly", "reason", 2000)
        stack_trace = _bounded_text(data.get("stack_trace"), "stack_trace", MAX_TRACE_CHARS)
        stderr_tail = _bounded_text(data.get("stderr_tail"), "stderr_tail", MAX_TEXT_CHARS)
        stack_signature = normalize_stack_trace(stack_trace, stderr_tail, reason)
        fingerprint = crash_fingerprint(
            revision=revision,
            process_name=process_name,
            stack_signature=stack_signature,
            exit_code=data.get("exit_code"),
            signal=data.get("signal"),
        )
        metadata = _bounded_json(data.get("metadata"), "crash metadata")
        core_metadata = _bounded_json(data.get("core_metadata"), "core metadata")
        resource_snapshot = _bounded_json(data.get("resource_snapshot"), "resource snapshot")
        now = utcnow()

        with self._lock:
            duplicate = self.store.query_one(
                "SELECT report_id FROM agent_crash_occurrences WHERE event_id = ?",
                (event_id,),
            )
            if duplicate is not None:
                return self._response(str(duplicate["report_id"]), event_id, duplicate=True)

            report = self.store.query_one(
                "SELECT * FROM agent_crash_reports WHERE fingerprint = ?", (fingerprint,)
            )
            report_id = str(report["id"]) if report is not None else new_id("crash")
            affected = json_loads(report["affected_agent_ids"], []) if report is not None else []
            affected_ids = sorted({str(value) for value in affected if value} | {agent_id})
            with self.store.transaction() as conn:
                if report is None:
                    conn.execute(
                        """
                        INSERT INTO agent_crash_reports (
                            id, fingerprint, process_name, revision, stack_signature,
                            status, occurrence_count, repair_attempt_count,
                            affected_agent_ids, repair_task_id,
                            first_seen_at, last_seen_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'open', 1, 0, ?, NULL, ?, ?, ?)
                        """,
                        (
                            report_id,
                            fingerprint,
                            process_name,
                            revision,
                            stack_signature,
                            json_dumps(affected_ids),
                            observed_at,
                            observed_at,
                            now,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE agent_crash_reports
                        SET occurrence_count = occurrence_count + 1,
                            affected_agent_ids = ?,
                            status = CASE WHEN status = 'needs_human' THEN status ELSE 'open' END,
                            last_seen_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dumps(affected_ids), observed_at, now, report_id),
                    )
                conn.execute(
                    """
                    INSERT INTO agent_crash_occurrences (
                        id, event_id, report_id, agent_id, observed_at, supervisor,
                        process_name, pid, exit_code, signal, reason, revision,
                        tree_sha, task_id, lease_id, stack_trace, stderr_tail,
                        core_reference, core_metadata, resource_snapshot, metadata,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("crashocc"),
                        event_id,
                        report_id,
                        agent_id,
                        observed_at,
                        supervisor,
                        process_name,
                        int(data["pid"]) if data.get("pid") is not None else None,
                        int(data["exit_code"]) if data.get("exit_code") is not None else None,
                        int(data["signal"]) if data.get("signal") is not None else None,
                        reason,
                        revision,
                        _bounded_text(data.get("tree_sha"), "tree_sha", 160),
                        _bounded_text(data.get("task_id"), "task_id", 160) or None,
                        _bounded_text(data.get("lease_id"), "lease_id", 160) or None,
                        stack_trace,
                        stderr_tail,
                        _bounded_text(data.get("core_reference"), "core_reference", 4096),
                        json_dumps(core_metadata),
                        json_dumps(resource_snapshot),
                        json_dumps(metadata),
                        now,
                    ),
                )

            task = self._ensure_repair_task(report_id, affected_ids)
            self.cp.observability.record_log(
                "agent.crash.observed",
                layer="supervisor",
                source=agent_id,
                subject_type="crash_report",
                subject_id=report_id,
                detail={
                    "fingerprint": fingerprint,
                    "revision": revision,
                    "process_name": process_name,
                    "repair_task_id": task.id if task is not None else None,
                    "affected_agent_ids": affected_ids,
                },
            )
            return self._response(report_id, event_id, duplicate=False)

    def list_reports(
        self, *, status: Optional[str] = None, agent_id: Optional[str] = None, limit: int = 100
    ) -> List[JsonDict]:
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("r.status = ?")
            params.append(str(status))
        if agent_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM agent_crash_occurrences o WHERE o.report_id = r.id AND o.agent_id = ?)"
            )
            params.append(str(agent_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        rows = self.store.query_all(
            "SELECT r.* FROM agent_crash_reports r%s ORDER BY r.last_seen_at DESC LIMIT ?" % where,
            tuple(params),
        )
        return [self._report_dict(row) for row in rows]

    def get_report(self, report_id: str) -> JsonDict:
        row = self.store.query_one("SELECT * FROM agent_crash_reports WHERE id = ?", (report_id,))
        if row is None:
            raise NotFoundError("crash report not found: %s" % report_id)
        result = self._report_dict(row)
        result["occurrences"] = [
            self._occurrence_dict(item)
            for item in self.store.query_all(
                "SELECT * FROM agent_crash_occurrences WHERE report_id = ? ORDER BY observed_at DESC",
                (report_id,),
            )
        ]
        return result

    def resolve(self, report_id: str, *, actor: str, reason: str) -> JsonDict:
        self.get_report(report_id)
        now = utcnow()
        self.store.execute(
            """
            UPDATE agent_crash_reports
            SET status = 'resolved', repair_task_id = NULL,
                repair_attempt_count = 0, updated_at = ?
            WHERE id = ?
            """,
            (now, report_id),
        )
        self.cp.observability.record_log(
            "agent.crash.resolved",
            layer="supervisor",
            source=actor,
            subject_type="crash_report",
            subject_id=report_id,
            detail={"reason": str(reason or "resolved")[:2000]},
        )
        return self.get_report(report_id)

    def tick(self, *, limit: int = 100) -> JsonDict:
        """Reconcile repair-task outcomes without relying on the crashed node."""
        rows = self.store.query_all(
            """
            SELECT * FROM agent_crash_reports
            WHERE repair_task_id IS NOT NULL AND status IN ('repairing', 'open')
            ORDER BY updated_at LIMIT ?
            """,
            (max(1, min(int(limit), 1000)),),
        )
        result: JsonDict = {
            "schema": "mac.agent_crash_repair_tick.v1",
            "examined": len(rows),
            "resolved": 0,
            "requeued": 0,
            "escalated": 0,
            "errors": [],
        }
        for row in rows:
            report_id = str(row["id"])
            try:
                task = self.cp.get_task(str(row["repair_task_id"]))
                if task.state == TaskState.COMPLETED.value:
                    self.store.execute(
                        "UPDATE agent_crash_reports SET status = 'resolved', updated_at = ? WHERE id = ?",
                        (utcnow(), report_id),
                    )
                    result["resolved"] += 1
                elif task.state in {TaskState.FAILED.value, TaskState.CANCELLED.value}:
                    attempts = int(row["repair_attempt_count"] or 0)
                    if attempts >= MAX_REPAIR_ATTEMPTS:
                        self.store.execute(
                            "UPDATE agent_crash_reports SET status = 'needs_human', updated_at = ? WHERE id = ?",
                            (utcnow(), report_id),
                        )
                        self.cp.record_notification(
                            "agent.crash.repair_exhausted",
                            "Crash repair exhausted: %s" % report_id,
                            "All %d autonomous repair attempts failed for fingerprint %s."
                            % (attempts, row["fingerprint"]),
                            channels=None,
                            metadata={"severity": "error", "crash_report_id": report_id},
                        )
                        result["escalated"] += 1
                    else:
                        self.store.execute(
                            "UPDATE agent_crash_reports SET status = 'open', updated_at = ? WHERE id = ?",
                            (utcnow(), report_id),
                        )
                        affected = json_loads(row["affected_agent_ids"], [])
                        self._ensure_repair_task(report_id, list(affected or []))
                        result["requeued"] += 1
            except Exception as exc:  # noqa: BLE001 - isolate incident reconciliation.
                result["errors"].append({"report_id": report_id, "error": str(exc)[:500]})
        return result

    def _ensure_repair_task(self, report_id: str, affected_ids: List[str]) -> Any:
        report = self.store.query_one(
            "SELECT * FROM agent_crash_reports WHERE id = ?", (report_id,)
        )
        if report is None:
            return None
        if report["status"] == "needs_human":
            return None
        prior_task = None
        if report["repair_task_id"]:
            try:
                prior_task = self.cp.get_task(str(report["repair_task_id"]))
            except NotFoundError:
                prior_task = None
        if prior_task is not None and prior_task.state in _ACTIVE_TASK_STATES:
            self.store.execute(
                "UPDATE agent_crash_reports SET status = 'repairing', updated_at = ? WHERE id = ?",
                (utcnow(), report_id),
            )
            metadata = ensure_json_object(prior_task.metadata)
            excluded = sorted(
                set(str(value) for value in metadata.get("excluded_agent_ids", []) if value)
                | set(affected_ids)
            )
            if excluded != metadata.get("excluded_agent_ids"):
                metadata["excluded_agent_ids"] = excluded
                metadata["crash_affected_agent_ids"] = excluded
                prior_task = self.cp.update_task(
                    prior_task.id, metadata=metadata, actor="crash-observer"
                )
            if prior_task.owner_agent_id in set(affected_ids) and prior_task.lease_id:
                # The peer originally assigned to repair the crash has now
                # exhibited the same fingerprint. Release its active lease so
                # an unaffected peer can take over instead of letting the
                # crashed process retain ownership until lease expiry.
                try:
                    prior_task = self.cp.release_lease(
                        prior_task.lease_id, str(prior_task.owner_agent_id)
                    )
                except Exception:  # noqa: BLE001 - lease may have raced renewal/expiry.
                    prior_task = self.cp.get_task(prior_task.id)
                try:
                    self.cp.dispatch_once()
                except Exception:  # noqa: BLE001 - durable open task remains ready.
                    pass
            return prior_task

        occurrence = self.store.query_one(
            "SELECT * FROM agent_crash_occurrences WHERE report_id = ? ORDER BY observed_at DESC LIMIT 1",
            (report_id,),
        )
        if occurrence is None:
            return None
        trace_excerpt = str(report["stack_signature"] or "")[-8000:]
        prior_clause = ""
        if prior_task is not None:
            prior_clause = (
                "\n\nThis crash recurred after repair task %s reached %s. Read that task's "
                "evidence and do not repeat an ineffective repair."
                % (prior_task.id, prior_task.state)
            )
        description = (
            "A supervisor outside the MAC worker observed an unexpected process exit.\n\n"
            "Crash report: %s\nFingerprint: %s\nRevision: %s\nProcess: %s\n"
            "Affected agents: %s\nExit code: %s\nSignal: %s\nCore reference: %s\n\n"
            "Normalized stack signature:\n%s%s\n\n"
            "Repair the root cause, add a regression test that reproduces the failure, and "
            "verify the corrected build on an unaffected agent before closing this task."
            % (
                report_id,
                report["fingerprint"],
                report["revision"],
                report["process_name"],
                ", ".join(affected_ids),
                occurrence["exit_code"],
                occurrence["signal"],
                occurrence["core_reference"] or "none",
                trace_excerpt,
                prior_clause,
            )
        )
        try:
            task = self.cp.create_task(
                "P0: repair MAC crash %s at %s"
                % (str(report["process_name"])[:50], str(report["revision"])[:12]),
                description=description,
                project="mac",
                priority=0,
                required_capabilities=["python", "ops"],
                metadata={
                    "origin": {"type": "crash_observer", "schema": CRASH_REPORT_SCHEMA},
                    "crash_report_id": report_id,
                    "crash_fingerprint": report["fingerprint"],
                    "crash_revision": report["revision"],
                    "crash_affected_agent_ids": affected_ids,
                    "excluded_agent_ids": affected_ids,
                    "self_heal": True,
                    "evidence_type": "crash_repair",
                    "prior_repair_task_id": prior_task.id if prior_task is not None else None,
                },
                actor="crash-observer",
            )
        except GeneratorSuppressed as exc:
            # Autonomous crash repair has been measured and is not producing
            # completed work. Stop manufacturing repair tasks, but do NOT drop
            # the crash: the report escalates to a human, which is the outcome
            # a suppressed repair channel should produce.
            self.store.execute(
                "UPDATE agent_crash_reports SET status = 'needs_human', updated_at = ? WHERE id = ?",
                (utcnow(), report_id),
            )
            self.cp.record_notification(
                "agent.crash.repair_suppressed",
                "Crash repair suppressed: %s" % report_id,
                "Autonomous repair for fingerprint %s was not filed: %s"
                % (report["fingerprint"], exc),
                channels=None,
                metadata={"severity": "warning", "crash_report_id": report_id},
            )
            return None
        self.store.execute(
            """
            UPDATE agent_crash_reports
            SET status = 'repairing', repair_task_id = ?,
                repair_attempt_count = repair_attempt_count + 1, updated_at = ?
            WHERE id = ?
            """,
            (task.id, utcnow(), report_id),
        )
        # Best-effort immediate dispatch. If every unaffected peer is busy, the
        # ordinary dispatcher will claim the task as soon as one becomes idle.
        try:
            self.cp.dispatch_once()
        except Exception:  # noqa: BLE001 - durable open task is the fallback.
            pass
        return task

    def _response(self, report_id: str, event_id: str, *, duplicate: bool) -> JsonDict:
        result = self.get_report(report_id)
        result["event_id"] = event_id
        result["duplicate"] = duplicate
        return result

    @staticmethod
    def _report_dict(row: Any) -> JsonDict:
        return {
            "schema": CRASH_REPORT_SCHEMA,
            "id": row["id"],
            "fingerprint": row["fingerprint"],
            "process_name": row["process_name"],
            "revision": row["revision"],
            "stack_signature": row["stack_signature"],
            "status": row["status"],
            "occurrence_count": int(row["occurrence_count"]),
            "repair_attempt_count": int(row["repair_attempt_count"]),
            "affected_agent_ids": json_loads(row["affected_agent_ids"], []),
            "repair_task_id": row["repair_task_id"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _occurrence_dict(row: Any) -> JsonDict:
        return {
            "schema": CRASH_OCCURRENCE_SCHEMA,
            "id": row["id"],
            "event_id": row["event_id"],
            "report_id": row["report_id"],
            "agent_id": row["agent_id"],
            "observed_at": row["observed_at"],
            "supervisor": row["supervisor"],
            "process_name": row["process_name"],
            "pid": row["pid"],
            "exit_code": row["exit_code"],
            "signal": row["signal"],
            "reason": row["reason"],
            "revision": row["revision"],
            "tree_sha": row["tree_sha"],
            "task_id": row["task_id"],
            "lease_id": row["lease_id"],
            "stack_trace": row["stack_trace"],
            "stderr_tail": row["stderr_tail"],
            "core_reference": row["core_reference"],
            "core_metadata": json_loads(row["core_metadata"], {}),
            "resource_snapshot": json_loads(row["resource_snapshot"], {}),
            "metadata": json_loads(row["metadata"], {}),
            "created_at": row["created_at"],
        }
