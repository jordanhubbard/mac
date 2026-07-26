"""Task lifecycle and dispatch services.

Implements the ledger and dispatch services that record task transitions and
emit ordered outbox side effects, using a monotonic sequence so drained events
preserve enqueue order.
"""

from __future__ import annotations

import itertools
import threading
from typing import Any, Dict, List, Optional

from mac.models import JsonDict, TaskTransitionOutbox, json_dumps, json_loads, new_id, utcnow

# Outbox rows from a single transition share an identical ``created_at``.
# ``list_outbox`` orders by ``created_at, id``; if ``id`` is a random uuid
# the secondary sort is non-deterministic and side effects (e.g. the beads
# ledger note vs the reopen --status note) can fire out of enqueue order.
# A process-wide monotonic counter baked into the id makes the id sort in
# enqueue order, so the drain order is stable. Store-agnostic, no schema
# change. The counter resets per process, which is fine because it only
# acts as a tiebreaker within the same created_at timestamp.
_outbox_seq = itertools.count()
_outbox_seq_lock = threading.Lock()


def _next_outbox_seq() -> int:
    with _outbox_seq_lock:
        return next(_outbox_seq)


class TaskLedgerService:
    """Small transactional helper for task lifecycle state.

    ControlPlane still exposes the compatibility API, but task lifecycle writes
    call through this helper so state changes, history, and side-effect intents
    are staged together.
    """

    def __init__(self, store: Any) -> None:
        self.store = store


    def enqueue_outbox(
        self,
        conn: Any,
        *,
        task_id: str,
        event_type: str,
        actor: str,
        from_state: Optional[str],
        to_state: Optional[str],
        detail: Optional[Dict[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> str:
        outbox_id = "tout_%016x_%s" % (_next_outbox_seq(), new_id("").lstrip("_"))
        conn.execute(
            """
            INSERT INTO task_transition_outbox (
                id, task_id, event_type, actor, from_state, to_state, detail,
                status, attempts, created_at, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL)
            """,
            (
                outbox_id,
                task_id,
                event_type,
                actor,
                from_state,
                to_state,
                json_dumps(detail or {}),
                created_at or utcnow(),
            ),
        )
        return outbox_id

    def list_outbox(
        self,
        *,
        status: str = "pending",
        task_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[TaskTransitionOutbox]:
        clauses = ["status = ?"]
        params: List[Any] = [status]
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        sql = (
            "SELECT * FROM task_transition_outbox WHERE "
            + " AND ".join(clauses)
            # ``rowid`` is a SQLite implicit column; Postgres doesn't
            # expose it. Both backends have a ``id TEXT PRIMARY KEY`` —
            # use that as the secondary tiebreaker so ordering is stable
            # across stores.
            + " ORDER BY created_at, id LIMIT ?"
        )
        params.append(min(max(1, int(limit)), 1000))
        return [self._from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def mark_outbox_processed(self, outbox_id: str, *, status: str = "delivered") -> None:
        self.store.execute(
            """
            UPDATE task_transition_outbox
            SET status = ?, attempts = attempts + 1, processed_at = ?
            WHERE id = ?
            """,
            (status, utcnow(), outbox_id),
        )

    def mark_outbox_failed(self, outbox_id: str, error: str) -> None:
        row = self.store.query_one(
            "SELECT detail FROM task_transition_outbox WHERE id = ?",
            (outbox_id,),
        )
        detail: JsonDict = json_loads(row["detail"], {}) if row is not None else {}
        detail["last_error"] = str(error)
        self.store.execute(
            """
            UPDATE task_transition_outbox
            SET status = 'failed', attempts = attempts + 1,
                processed_at = ?, detail = ?
            WHERE id = ?
            """,
            (utcnow(), json_dumps(detail), outbox_id),
        )

    def _from_row(self, row: Any) -> TaskTransitionOutbox:
        return TaskTransitionOutbox(
            id=row["id"],
            task_id=row["task_id"],
            event_type=row["event_type"],
            actor=row["actor"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            detail=json_loads(row["detail"], {}),
            status=row["status"],
            attempts=int(row["attempts"]),
            created_at=row["created_at"],
            processed_at=row["processed_at"],
        )


class DispatchService:
    """Dispatch boundary for claim policy orchestration."""

    def __init__(self, control_plane: Any) -> None:
        self.control_plane = control_plane

    def dispatch_once(self, *args: Any, **kwargs: Any) -> Optional[JsonDict]:
        return self.control_plane._dispatch_once_impl(*args, **kwargs)

    def claim_next_for_agent(self, *args: Any, **kwargs: Any) -> Optional[JsonDict]:
        return self.control_plane._claim_next_for_agent_impl(*args, **kwargs)
