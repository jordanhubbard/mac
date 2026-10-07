"""The task board: one ordered conversation per task.

A coding agent working a task, the people watching it, and the hub all write
to the same per-task stream (``task_messages``). The agent's harness reads it
between tool calls and puts anything new into the agent's context, so a human
can redirect a running agent, answer its question, or ask what it is doing,
without stopping it. The console reads the same stream to show the work live.

Rows are append-only. ``id`` is the read cursor: a reader passes the last id it
saw and gets everything after it, in order, so nothing is skipped.

Who may write what is decided here, not by the caller:

* an agent posts what it is doing (``status``, ``activity``), what it needs
  (``question``), what it has finished (``done``), or a plain ``message``;
* a human posts direction (``directive``), an ``answer`` to a question, or a
  ``message``;
* the hub posts anything, including ``nudge`` and ``verdict``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from mac.models import NotFoundError, ValidationError

AUTHOR_KINDS = ("agent", "human", "hub")

KINDS = (
    "message",
    "status",
    "activity",
    "question",
    "answer",
    "directive",
    "nudge",
    "verdict",
    "done",
)

#: What each kind of author may post. The hub posts on the system's behalf.
ALLOWED_KINDS: Mapping[str, frozenset] = {
    "agent": frozenset({"message", "status", "activity", "question", "done"}),
    "human": frozenset({"message", "answer", "directive"}),
    "hub": frozenset(KINDS),
}

#: Kinds that carry something for the agent to act on. The agent's harness
#: delivers these into its context; it never echoes the agent's own posts.
FOR_AGENT_KINDS = frozenset({"message", "answer", "directive", "nudge", "verdict"})

MAX_BODY_CHARS = 16_000
MAX_METADATA_CHARS = 16_000
MAX_LIST_LIMIT = 500


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class TaskMessage:
    id: int
    task_id: str
    author_kind: str
    author: str
    kind: str
    body: str
    reply_to: Optional[int]
    metadata: Dict[str, Any]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "author_kind": self.author_kind,
            "author": self.author,
            "kind": self.kind,
            "body": self.body,
            "reply_to": self.reply_to,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> "TaskMessage":
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        reply_to = row["reply_to"]
        return cls(
            id=int(row["id"]),
            task_id=str(row["task_id"]),
            author_kind=str(row["author_kind"]),
            author=str(row["author"]),
            kind=str(row["kind"]),
            body=str(row["body"]),
            reply_to=int(reply_to) if reply_to is not None else None,
            metadata=metadata if isinstance(metadata, dict) else {},
            created_at=str(row["created_at"]),
        )


class TaskBoard:
    """Append to and read a task's message stream."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def post(
        self,
        task_id: str,
        *,
        author_kind: str,
        author: str,
        kind: str,
        body: str,
        reply_to: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> TaskMessage:
        task_id = str(task_id or "").strip()
        author = str(author or "").strip()
        body = str(body or "").strip()
        if author_kind not in AUTHOR_KINDS:
            raise ValidationError("author_kind must be one of %s" % ", ".join(AUTHOR_KINDS))
        if kind not in KINDS:
            raise ValidationError("kind must be one of %s" % ", ".join(KINDS))
        if kind not in ALLOWED_KINDS[author_kind]:
            raise ValidationError("a %s may not post a %s message" % (author_kind, kind))
        if not author:
            raise ValidationError("author is required")
        if not body:
            raise ValidationError("body is required")
        if len(body) > MAX_BODY_CHARS:
            raise ValidationError("body exceeds %d characters" % MAX_BODY_CHARS)
        metadata_json = json.dumps(dict(metadata or {}), sort_keys=True, default=str)
        if len(metadata_json) > MAX_METADATA_CHARS:
            raise ValidationError("metadata exceeds %d characters" % MAX_METADATA_CHARS)
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
                raise NotFoundError("task %s not found" % task_id)
            if reply_to is not None:
                parent = conn.execute(
                    "SELECT task_id FROM task_messages WHERE id = ?", (int(reply_to),)
                ).fetchone()
                if parent is None or str(parent["task_id"]) != task_id:
                    raise ValidationError("reply_to must name a message on the same task")
            row = conn.execute(
                """
                INSERT INTO task_messages (
                    task_id, author_kind, author, kind, body, reply_to, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id, task_id, author_kind, author, kind, body, reply_to,
                          metadata, created_at
                """,
                (
                    task_id,
                    author_kind,
                    author,
                    kind,
                    body,
                    int(reply_to) if reply_to is not None else None,
                    metadata_json,
                    _utcnow(),
                ),
            ).fetchone()
        return TaskMessage.from_row(row)

    def list(
        self,
        task_id: str,
        *,
        after: int = 0,
        limit: int = 200,
        kinds: Optional[List[str]] = None,
    ) -> List[TaskMessage]:
        limit = max(1, min(int(limit or 200), MAX_LIST_LIMIT))
        sql = "SELECT * FROM task_messages WHERE task_id = ? AND id > ?"
        params: List[Any] = [str(task_id), int(after or 0)]
        wanted = [kind for kind in (kinds or []) if kind in KINDS]
        if wanted:
            sql += " AND kind IN (%s)" % ", ".join("?" for _ in wanted)
            params.extend(wanted)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        return [TaskMessage.from_row(row) for row in self.store.query_all(sql, params)]

    def get(self, message_id: int) -> TaskMessage:
        row = self.store.query_one("SELECT * FROM task_messages WHERE id = ?", (int(message_id),))
        if row is None:
            raise NotFoundError("task message %s not found" % message_id)
        return TaskMessage.from_row(row)
