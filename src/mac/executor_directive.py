"""Task-scoped directive contracts and the durable executor-owned queue.

Incident task_60be7f29: two authentic operator ``human.directive.v1`` messages
carried ``task_id=null`` and were consumed by an agent's *persona* sandbox — a
chat turn with no repository worktree and no live task lease. The real task
executor, which DID hold the worktree, never saw them, yet the persona's Slack
reply looked like task progress.

This module makes a task-scoped directive a first-class object rather than a
task id buried in prose:

  * :func:`task_ownership_verdict` is the pure, store-free decision the hub and
    worker both evaluate — an executor-scoped directive is deliverable only
    when the *target* agent holds a *current* (non-terminal, unexpired) lease
    for the cited task. It fails closed with a structured reason otherwise.

  * :class:`ExecutorDirectiveQueue` is the durable, executor-owned inbox the
    worker routes a verified task-scoped directive into. A persona/chat turn
    can never write here, so consuming from this queue is proof the *active
    executor* received the directive (with the stream id as provenance).

Kept free of network, clock-source, and worker dependencies so the hub, the
worker, and the tests all agree on one contract and one digest.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

JsonDict = Dict[str, Any]

# Contract identifiers -------------------------------------------------------
# The header/envelope marker set on a task-scoped human directive so any
# receiver can tell an executor-bound directive from ordinary operator speech.
EXECUTOR_SCOPED_HEADER = "executor_scoped"
DELIVERY_TARGET_HEADER = "delivery_target"
DELIVERY_TARGET_EXECUTOR = "executor"
DELIVERY_TARGET_PERSONA = "persona"

# The executor-delivery acknowledgement — distinct from peer.reply.v1 so a
# conversation mirror never mistakes a persona chat turn for proof that the
# active task run consumed a directive.
EXECUTOR_ACK_TOPIC = "task.directive.ack.v1"
EXECUTOR_ACK_SCHEMA = "mac.task.directive_ack.v1"
EXECUTOR_ACK_CONTENT_TYPE = "application/vnd.mac.task-directive-ack+json"

# The reserved operator persona agent id (mirrors
# ControlPlane.OPERATOR_PERSONA_AGENT_ID) so the worker can address executor
# acks back to the directive's origin without importing the services layer.
OPERATOR_PERSONA_AGENT_ID = "agent_operator"

# delivery_kind values carried on both peer replies and executor acks so the
# mirror layer can label them without re-deriving provenance.
DELIVERY_KIND_EXECUTOR = "task_executor"
DELIVERY_KIND_PERSONA = "persona"

# Terminal task states in which no executor can own a live lease. Mirrors
# mac.models.TERMINAL_TASK_STATES without importing it (kept dependency-free).
_TERMINAL_TASK_STATES = frozenset({"done", "failed", "cancelled"})


@dataclass(frozen=True)
class TaskOwnershipVerdict:
    """Whether an executor-scoped directive may be delivered to *target*."""

    deliverable: bool
    status: str
    reason: Optional[str]
    task_id: str
    target_agent_id: str
    owner_agent_id: Optional[str]
    lease_id: Optional[str]

    def to_dict(self) -> JsonDict:
        return {
            "schema": "mac.task.directive_ownership.v1",
            "deliverable": self.deliverable,
            "status": self.status,
            "reason": self.reason,
            "task_id": self.task_id,
            "target_agent_id": self.target_agent_id,
            "owner_agent_id": self.owner_agent_id,
            "lease_id": self.lease_id,
        }


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def task_ownership_verdict(
    *,
    task_id: str,
    target_agent_id: str,
    task_found: bool,
    task_state: Optional[str],
    owner_agent_id: Optional[str],
    lease_id: Optional[str],
    leased_until: Optional[str],
    now: datetime,
) -> TaskOwnershipVerdict:
    """Decide whether *target* owns a current lease for *task* (fail closed).

    Structured statuses (acceptance criterion 4):
      * ``no_task``           — the cited task does not exist.
      * ``no_executor``       — the task has no owning agent / active lease.
      * ``agent_task_mismatch`` — a different agent owns the lease.
      * ``lease_expired``     — the lease exists but its window has passed
                                (or the task is in a terminal state).
      * ``deliverable``       — target holds a current lease; deliver.
    """
    task_id = str(task_id or "")
    target_agent_id = str(target_agent_id or "")

    def _verdict(status: str, reason: Optional[str], deliverable: bool) -> TaskOwnershipVerdict:
        return TaskOwnershipVerdict(
            deliverable=deliverable,
            status=status,
            reason=reason,
            task_id=task_id,
            target_agent_id=target_agent_id,
            owner_agent_id=owner_agent_id,
            lease_id=lease_id,
        )

    if not task_found:
        return _verdict("no_task", "task not found: %s" % task_id, False)
    if not owner_agent_id or not lease_id:
        return _verdict(
            "no_executor",
            "task %s has no active executor lease" % task_id,
            False,
        )
    if str(task_state or "") in _TERMINAL_TASK_STATES:
        return _verdict(
            "lease_expired",
            "task %s is in terminal state %s; no live executor" % (task_id, task_state),
            False,
        )
    if owner_agent_id != target_agent_id:
        return _verdict(
            "agent_task_mismatch",
            "agent %s does not own task %s (owner is %s)"
            % (target_agent_id, task_id, owner_agent_id),
            False,
        )
    deadline = _parse_iso(leased_until)
    if deadline is not None and now >= deadline:
        return _verdict(
            "lease_expired",
            "lease %s for task %s expired at %s" % (lease_id, task_id, leased_until),
            False,
        )
    return _verdict("deliverable", None, True)


@dataclass(frozen=True)
class ExecutorDirectiveRecord:
    """One durable, executor-owned directive entry."""

    stream_id: str
    task_id: str
    correlation_id: str
    message: str
    issued_by: str
    enqueued_at: str
    consumed_at: Optional[str] = None

    def to_dict(self) -> JsonDict:
        return {
            "schema": "mac.task.directive_record.v1",
            "stream_id": self.stream_id,
            "task_id": self.task_id,
            "correlation_id": self.correlation_id,
            "message": self.message,
            "issued_by": self.issued_by,
            "enqueued_at": self.enqueued_at,
            "consumed_at": self.consumed_at,
        }

    @classmethod
    def from_dict(cls, data: JsonDict) -> "ExecutorDirectiveRecord":
        return cls(
            stream_id=str(data.get("stream_id") or ""),
            task_id=str(data.get("task_id") or ""),
            correlation_id=str(data.get("correlation_id") or ""),
            message=str(data.get("message") or ""),
            issued_by=str(data.get("issued_by") or "operator"),
            enqueued_at=str(data.get("enqueued_at") or ""),
            consumed_at=(str(data["consumed_at"]) if data.get("consumed_at") else None),
        )


class ExecutorDirectiveQueue:
    """A durable JSON-file queue owned by the active task executor.

    The worker (which holds the lease and the repository worktree) enqueues a
    verified task-scoped directive here; the executor drains it. A persona/chat
    turn has neither this path nor the lease, so it can never write here —
    consuming an entry is durable proof the *executor* received the directive.

    Writes are atomic (temp file + os.replace) and de-duplicated by
    ``stream_id`` so a re-polled stream is enqueued at most once.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> List[ExecutorDirectiveRecord]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            return []
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            return []
        records: List[ExecutorDirectiveRecord] = []
        for entry in entries:
            if isinstance(entry, dict):
                records.append(ExecutorDirectiveRecord.from_dict(entry))
        return records

    def _write(self, records: List[ExecutorDirectiveRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "mac.task.directive_queue.v1",
            "entries": [record.to_dict() for record in records],
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def enqueue(self, record: ExecutorDirectiveRecord) -> bool:
        """Append *record* unless its stream id is already queued.

        Returns True if the record was newly enqueued, False if it was a
        duplicate (idempotent re-poll).
        """
        records = self._read()
        if any(existing.stream_id == record.stream_id for existing in records):
            return False
        records.append(record)
        self._write(records)
        return True

    def pending(self) -> List[ExecutorDirectiveRecord]:
        return [record for record in self._read() if record.consumed_at is None]

    def all_records(self) -> List[ExecutorDirectiveRecord]:
        return self._read()

    def mark_consumed(self, stream_id: str, consumed_at: str) -> Optional[ExecutorDirectiveRecord]:
        """Stamp *stream_id* consumed; returns the updated record or None."""
        records = self._read()
        updated: Optional[ExecutorDirectiveRecord] = None
        rebuilt: List[ExecutorDirectiveRecord] = []
        for record in records:
            if record.stream_id == stream_id and record.consumed_at is None:
                updated = ExecutorDirectiveRecord(
                    stream_id=record.stream_id,
                    task_id=record.task_id,
                    correlation_id=record.correlation_id,
                    message=record.message,
                    issued_by=record.issued_by,
                    enqueued_at=record.enqueued_at,
                    consumed_at=consumed_at,
                )
                rebuilt.append(updated)
            else:
                rebuilt.append(record)
        if updated is not None:
            self._write(rebuilt)
        return updated


def executor_ack_payload(
    *,
    from_agent_id: str,
    to_agent_id: str,
    task_id: str,
    stream_id: str,
    status: str,
    reason: Optional[str] = None,
    correlation_id: Optional[str] = None,
    consumed_at: Optional[str] = None,
    enqueued_at: Optional[str] = None,
) -> JsonDict:
    """Build a task.directive.ack.v1 payload proving executor delivery.

    ``status`` is one of ``delivered`` (enqueued to the executor-owned queue),
    ``consumed`` (the executor drained it), or a fail-closed status from
    :func:`task_ownership_verdict` (``no_executor``, ``agent_task_mismatch``,
    ``lease_expired``, ``no_task``, ``unsupported_runtime``).

    ``delivery_kind`` is always ``task_executor`` — this schema is *only* ever
    minted by the executor seam, never by a persona chat turn.
    """
    payload: JsonDict = {
        "schema": EXECUTOR_ACK_SCHEMA,
        "from_agent_id": from_agent_id,
        "to_agent_id": to_agent_id,
        "delivery_kind": DELIVERY_KIND_EXECUTOR,
        "task_id": task_id,
        "stream_id": stream_id,
        "status": status,
    }
    if reason:
        payload["reason"] = str(reason)[:2000]
    if correlation_id:
        payload["correlation_id"] = correlation_id
    if enqueued_at:
        payload["enqueued_at"] = enqueued_at
    if consumed_at:
        payload["consumed_at"] = consumed_at
    return payload
