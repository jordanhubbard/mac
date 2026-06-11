"""Agent message service (control-channel messages).

Distinct from AgentBus: agentbus carries opaque typed content blobs,
``messages`` carries small, schema-validated control messages between
agents (help requests, evidence requests, review requests, status updates,
nudges, decision records).

Message payloads are NOT an execution channel. The validator rejects
payload keys whose lowercase form is a known execution verb (``command``,
``exec``, ``script``, ...) so a misbehaving agent cannot smuggle a job-spec
through this channel.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from mac.models import (
    Agent,
    AgentMessage,
    MessageStatus,
    MessageType,
    NotFoundError,
    Task,
    ValidationError,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)

FORBIDDEN_MESSAGE_KEYS = {
    "argv",
    "cmd",
    "code",
    "command",
    "exec",
    "executable",
    "powershell",
    "script",
    "shell",
}

MESSAGE_TYPE_REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    MessageType.HELP_REQUEST.value: ("question",),
    MessageType.EVIDENCE_REQUEST.value: ("task_id",),
    MessageType.STATUS_UPDATE.value: ("status",),
    MessageType.REVIEW_REQUEST.value: ("task_id", "review_id"),
    MessageType.REVIEW_RESULT.value: ("task_id", "status"),
    MessageType.NUDGE.value: ("task_id",),
    MessageType.DECISION_RECORD.value: ("summary",),
}
SYSTEM_SENDERS = {"dispatcher", "notifier"}


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


class MessagingService:
    """Per-agent control-message queue with schema validation.

    System sender IDs bypass the get_agent existence check because no row
    exists for control-plane generated messages.
    """

    def __init__(
        self,
        store: Any,
        *,
        get_agent: Callable[[str], Agent],
        get_task: Callable[[str], Task],
    ) -> None:
        self.store = store
        self._get_agent = get_agent
        self._get_task = get_task

    # Public API ---------------------------------------------------------

    def send_message(
        self,
        sender_agent_id: str,
        recipient_agent_id: Optional[str],
        message_type: str,
        payload: Dict[str, Any],
        task_id: Optional[str] = None,
    ) -> AgentMessage:
        if sender_agent_id not in SYSTEM_SENDERS:
            self._get_agent(sender_agent_id)
        if recipient_agent_id is not None:
            self._get_agent(recipient_agent_id)
        if task_id is not None:
            self._get_task(task_id)
        message_type_value = _state_value(message_type)
        try:
            MessageType(message_type_value)
        except ValueError:
            raise ValidationError("unsupported message type: %s" % message_type)
        self._validate_payload(message_type_value, payload)
        now = utcnow()
        message_id = new_id("msg")
        self.store.execute(
            """
            INSERT INTO messages (
                id, sender_agent_id, recipient_agent_id, task_id, message_type,
                payload, status, created_at, delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                message_id,
                sender_agent_id,
                recipient_agent_id,
                task_id,
                message_type_value,
                json_dumps(payload),
                MessageStatus.QUEUED.value,
                now,
            ),
        )
        return self.get_message(message_id)

    def get_message(self, message_id: str) -> AgentMessage:
        row = self.store.query_one("SELECT * FROM messages WHERE id = ?", (message_id,))
        if row is None:
            raise NotFoundError("message not found: %s" % message_id)
        return self._from_row(row)

    def has_queued_message(
        self,
        *,
        recipient_agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        message_type: Optional[str] = None,
        payload_contains: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if recipient_agent_id is not None:
            self._get_agent(recipient_agent_id)
        if task_id is not None:
            self._get_task(task_id)
        clauses = ["status = ?"]
        params: List[Any] = [MessageStatus.QUEUED.value]
        if recipient_agent_id is not None:
            clauses.append("recipient_agent_id = ?")
            params.append(recipient_agent_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if message_type is not None:
            message_type_value = _state_value(message_type)
            try:
                MessageType(message_type_value)
            except ValueError:
                raise ValidationError("unsupported message type: %s" % message_type)
            clauses.append("message_type = ?")
            params.append(message_type_value)
        rows = self.store.query_all(
            "SELECT * FROM messages WHERE %s ORDER BY created_at, id"
            % " AND ".join(clauses),
            tuple(params),
        )
        if not payload_contains:
            return bool(rows)
        for row in rows:
            payload = json_loads(row["payload"], {})
            if all(payload.get(key) == value for key, value in payload_contains.items()):
                return True
        return False

    def deliver_messages(self, agent_id: str, limit: int = 50) -> List[AgentMessage]:
        self._get_agent(agent_id)
        # mac-4pkm: do SELECT + UPDATE inside one transaction so two
        # concurrent deliver_messages calls to the same recipient can't
        # both see a queued row and both mark it delivered. The guarded
        # UPDATE (status = 'queued' in WHERE) means the loser's UPDATE
        # affects 0 rows and the row is excluded from its returned list.
        now = utcnow()
        delivered_ids: List[str] = []
        with self.store.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE status = ? AND (recipient_agent_id = ? OR recipient_agent_id IS NULL)
                ORDER BY created_at, id
                LIMIT ?
                """,
                (MessageStatus.QUEUED.value, agent_id, int(limit)),
            ).fetchall()
            for row in rows:
                cur = conn.execute(
                    "UPDATE messages SET status = ?, delivered_at = ? WHERE id = ? AND status = ?",
                    (MessageStatus.DELIVERED.value, now, row["id"], MessageStatus.QUEUED.value),
                )
                if cur.rowcount == 1:
                    delivered_ids.append(row["id"])
        return [self.get_message(mid) for mid in delivered_ids]

    def list_messages(
        self,
        agent_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[AgentMessage]:
        limit_value = None if limit is None else max(0, int(limit))
        if limit_value == 0:
            return []
        if agent_id:
            if limit_value is None:
                rows = self.store.query_all(
                    "SELECT * FROM messages WHERE recipient_agent_id = ? ORDER BY created_at, id",
                    (agent_id,),
                )
            else:
                rows = list(
                    reversed(
                        self.store.query_all(
                            """
                            SELECT * FROM messages
                            WHERE recipient_agent_id = ?
                            ORDER BY created_at DESC, id DESC
                            LIMIT ?
                            """,
                            (agent_id, limit_value),
                        )
                    )
                )
        elif limit_value is None:
            rows = self.store.query_all("SELECT * FROM messages ORDER BY created_at, id")
        else:
            rows = list(
                reversed(
                    self.store.query_all(
                        """
                        SELECT * FROM messages
                        ORDER BY created_at DESC, id DESC
                        LIMIT ?
                        """,
                        (limit_value,),
                    )
                )
            )
        return [self._from_row(row) for row in rows]

    # Validation ---------------------------------------------------------

    def _validate_payload(self, message_type: str, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValidationError("message payload must be a JSON object")
        required = MESSAGE_TYPE_REQUIRED_FIELDS.get(message_type, ())
        missing = [field for field in required if payload.get(field) in (None, "")]
        if missing:
            raise ValidationError(
                "message %s payload missing required field(s): %s"
                % (message_type, ",".join(missing))
            )
        self._check_json_safe(payload, ())

    def _check_json_safe(self, value: Any, path: Sequence[str]) -> None:
        """Reject non-JSON-serializable payloads and known execution keys.

        Workers consume messages as structured data and look up durable
        tasks from the ledger. Message payloads are not an execution
        channel — keys like ``command``/``script``/``exec`` are refused at
        the boundary.
        """
        if isinstance(value, dict):
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ValidationError(
                        "message payload keys must be strings at %s" % ".".join(path)
                    )
                key_path = path + (key,)
                if key.lower() in FORBIDDEN_MESSAGE_KEYS:
                    raise ValidationError(
                        "message payload cannot contain execution key: %s"
                        % ".".join(key_path)
                    )
                self._check_json_safe(nested, key_path)
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                self._check_json_safe(nested, path + (str(index),))
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise ValidationError(
                "message payload contains non-JSON value at %s" % ".".join(path)
            )

    # Row hydration ------------------------------------------------------

    def _from_row(self, row: Any) -> AgentMessage:
        return AgentMessage(
            row["id"],
            row["sender_agent_id"],
            row["recipient_agent_id"],
            row["task_id"],
            row["message_type"],
            json_loads(row["payload"], {}),
            row["status"],
            row["created_at"],
            row["delivered_at"],
        )
