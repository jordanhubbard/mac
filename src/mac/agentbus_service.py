"""AgentBus typed-content stream service.

Owns the ``agentbus_streams`` and ``agentbus_chunks`` tables. Streams are
agent-to-agent: a sender opens a stream toward a recipient, appends chunks,
and finally closes it. Recipients (and the sender) can read chunks back via
a sequence cursor. Authorization is membership-based: only the sender and
the named recipient can read; only the sender can write/close.

Validation guarantees:
* Stream ID, topic, content-type are bounded and shape-checked.
* Each chunk is JSON-serialized and capped at 256 KB.
* Chunks are sequenced with a UNIQUE(stream_id, sequence) constraint; the
  per-chunk INSERT runs inside ``store.transaction()`` so concurrent
  appenders are serialized by the store's BEGIN IMMEDIATE lock.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional

from mac.models import (
    AgentBusChunk,
    AgentBusStream,
    AgentBusStreamStatus,
    AuthorizationError,
    JsonDict,
    NotFoundError,
    ValidationError,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.observability_service import ObservabilityService

AGENTBUS_PAYLOAD_ENCODINGS = {"json", "text", "base64"}
AGENTBUS_TYPED_CONTENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9.+_/-]*(;[A-Za-z0-9_.+-]+=[A-Za-z0-9_.+-]+)*$"
)
AGENTBUS_STREAM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
AGENTBUS_TOPIC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,127}$")
AGENTBUS_MAX_CHUNK_BYTES = 256 * 1024


def _state_value(state: Any) -> str:
    return state.value if hasattr(state, "value") else str(state)


class AgentBusService:
    def __init__(self, store: Any, observability: ObservabilityService) -> None:
        self.store = store
        self.observability = observability

    # Public API ---------------------------------------------------------

    def open_stream(
        self,
        sender_agent_id: str,
        recipient_agent_id: Optional[str] = None,
        content_type: str = "application/json",
        topic: str = "content",
        headers: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        stream_id: Optional[str] = None,
    ) -> AgentBusStream:
        self._require_agent(sender_agent_id)
        if not recipient_agent_id:
            raise ValidationError("agentbus stream requires a recipient_agent_id")
        self._require_agent(recipient_agent_id)
        if task_id is not None:
            self._require_task(task_id)
        self._validate_content_type(content_type)
        topic_value = self._validate_topic(topic)
        headers_obj = ensure_json_object(headers)
        headers_json = json_dumps(headers_obj)
        if stream_id is None:
            sid = new_id("bus")
        else:
            sid = stream_id.strip() if isinstance(stream_id, str) else ""
            if not AGENTBUS_STREAM_ID_RE.match(sid):
                raise ValidationError("invalid agentbus stream_id: %s" % stream_id)
        if self.store.query_one("SELECT id FROM agentbus_streams WHERE id = ?", (sid,)):
            raise ValidationError("agentbus stream already exists: %s" % sid)
        now = utcnow()
        self.store.execute(
            """
            INSERT INTO agentbus_streams (
                id, sender_agent_id, recipient_agent_id, task_id, topic,
                content_type, headers, status, created_at, updated_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                sid,
                sender_agent_id,
                recipient_agent_id,
                task_id,
                topic_value,
                content_type,
                headers_json,
                AgentBusStreamStatus.OPEN.value,
                now,
                now,
            ),
        )
        self.observability.record_log(
            "agentbus.stream.opened",
            layer="agentbus",
            source=sender_agent_id,
            subject_type="agentbus_stream",
            subject_id=sid,
            detail={
                "sender_agent_id": sender_agent_id,
                "recipient_agent_id": recipient_agent_id,
                "task_id": task_id,
                "topic": topic_value,
                "content_type": content_type,
                "header_keys": sorted(headers_obj.keys()),
            },
        )
        return self.get_stream(sid)

    def append_chunk(
        self,
        stream_id: str,
        sender_agent_id: str,
        payload: Any = None,
        content_type: Optional[str] = None,
        payload_encoding: str = "json",
        final: bool = False,
    ) -> AgentBusChunk:
        self._require_agent(sender_agent_id)
        payload_json = self._serialize_payload(payload, payload_encoding)
        chunk_id = new_id("chunk")
        now = utcnow()
        with self.store.transaction() as conn:
            stream_row = conn.execute(
                "SELECT * FROM agentbus_streams WHERE id = ?",
                (stream_id,),
            ).fetchone()
            if stream_row is None:
                raise NotFoundError("agentbus stream not found: %s" % stream_id)
            if stream_row["sender_agent_id"] != sender_agent_id:
                raise AuthorizationError("only the stream sender can append chunks")
            if stream_row["status"] != AgentBusStreamStatus.OPEN.value:
                raise ValidationError("agentbus stream is not open: %s" % stream_id)
            chunk_content_type = content_type or stream_row["content_type"]
            self._validate_content_type(chunk_content_type)
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM agentbus_chunks WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            conn.execute(
                """
                INSERT INTO agentbus_chunks (
                    id, stream_id, sequence, sender_agent_id, content_type,
                    payload, payload_encoding, size_bytes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk_id,
                    stream_id,
                    sequence,
                    sender_agent_id,
                    chunk_content_type,
                    payload_json,
                    payload_encoding,
                    len(payload_json.encode("utf-8")),
                    now,
                ),
            )
            if final:
                conn.execute(
                    """
                    UPDATE agentbus_streams
                    SET status = ?, updated_at = ?, closed_at = ?
                    WHERE id = ?
                    """,
                    (AgentBusStreamStatus.CLOSED.value, now, now, stream_id),
                )
            else:
                conn.execute(
                    "UPDATE agentbus_streams SET updated_at = ? WHERE id = ?",
                    (now, stream_id),
                )
        chunk = self.get_chunk(chunk_id)
        self.observability.record_log(
            "agentbus.chunk.appended",
            layer="agentbus",
            source=sender_agent_id,
            subject_type="agentbus_stream",
            subject_id=stream_id,
            detail={
                "chunk_id": chunk.id,
                "sequence": chunk.sequence,
                "sender_agent_id": sender_agent_id,
                "content_type": chunk.content_type,
                "payload_encoding": chunk.payload_encoding,
                "size_bytes": chunk.size_bytes,
                "final": bool(final),
            },
        )
        return chunk

    def close_stream(
        self,
        stream_id: str,
        sender_agent_id: str,
        status: str = AgentBusStreamStatus.CLOSED.value,
    ) -> AgentBusStream:
        stream = self.get_stream(stream_id)
        self._require_agent(sender_agent_id)
        if stream.sender_agent_id != sender_agent_id:
            raise AuthorizationError("only the stream sender can close the stream")
        status_value = _state_value(status)
        if status_value == AgentBusStreamStatus.OPEN.value:
            raise ValidationError("agentbus close status cannot be open")
        try:
            AgentBusStreamStatus(status_value)
        except ValueError:
            raise ValidationError("unsupported agentbus stream status: %s" % status)
        if stream.status != AgentBusStreamStatus.OPEN.value:
            if stream.status == status_value:
                return stream
            raise ValidationError("agentbus stream already closed: %s" % stream_id)
        now = utcnow()
        self.store.execute(
            """
            UPDATE agentbus_streams
            SET status = ?, updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            (status_value, now, now, stream_id),
        )
        self.observability.record_log(
            "agentbus.stream.closed",
            layer="agentbus",
            source=sender_agent_id,
            subject_type="agentbus_stream",
            subject_id=stream_id,
            detail={"sender_agent_id": sender_agent_id, "status": status_value},
        )
        return self.get_stream(stream_id)

    def get_stream(self, stream_id: str) -> AgentBusStream:
        row = self.store.query_one("SELECT * FROM agentbus_streams WHERE id = ?", (stream_id,))
        if row is None:
            raise NotFoundError("agentbus stream not found: %s" % stream_id)
        return self._stream_from_row(row)

    def get_chunk(self, chunk_id: str) -> AgentBusChunk:
        row = self.store.query_one("SELECT * FROM agentbus_chunks WHERE id = ?", (chunk_id,))
        if row is None:
            raise NotFoundError("agentbus chunk not found: %s" % chunk_id)
        return self._chunk_from_row(row)

    def list_streams(
        self,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[AgentBusStream]:
        clauses: List[str] = []
        params: List[Any] = []
        if agent_id is not None:
            self._require_agent(agent_id)
            clauses.append("(sender_agent_id = ? OR recipient_agent_id = ?)")
            params.extend([agent_id, agent_id])
        if status is not None:
            status_value = _state_value(status)
            try:
                AgentBusStreamStatus(status_value)
            except ValueError:
                raise ValidationError("unsupported agentbus stream status: %s" % status)
            clauses.append("status = ?")
            params.append(status_value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        rows = self.store.query_all(
            "SELECT * FROM agentbus_streams%s ORDER BY updated_at DESC, id LIMIT ?" % where,
            tuple(params),
        )
        return [self._stream_from_row(row) for row in rows]

    def assert_authorized(self, agent_id: str, stream_id: str) -> AgentBusStream:
        self._require_agent(agent_id)
        stream = self.get_stream(stream_id)
        if not self._authorized(stream, agent_id):
            raise AuthorizationError("agent is not authorized for agentbus stream")
        return stream

    def read_chunks(
        self,
        agent_id: str,
        stream_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> List[AgentBusChunk]:
        self.assert_authorized(agent_id, stream_id)
        rows = self.store.query_all(
            """
            SELECT * FROM agentbus_chunks
            WHERE stream_id = ? AND sequence > ?
            ORDER BY sequence
            LIMIT ?
            """,
            (
                stream_id,
                max(0, int(after_sequence)),
                max(1, min(int(limit), 1000)),
            ),
        )
        chunks = [self._chunk_from_row(row) for row in rows]
        if chunks:
            self.observability.record_log(
                "agentbus.chunks.read",
                layer="agentbus",
                source=agent_id,
                subject_type="agentbus_stream",
                subject_id=stream_id,
                detail={
                    "agent_id": agent_id,
                    "after_sequence": max(0, int(after_sequence)),
                    "count": len(chunks),
                    "last_sequence": chunks[-1].sequence,
                },
            )
        return chunks

    def publish(
        self,
        sender_agent_id: str,
        recipient_agent_id: Optional[str] = None,
        content_type: str = "application/json",
        payload: Any = None,
        topic: str = "content",
        headers: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        payload_encoding: str = "json",
    ) -> JsonDict:
        # Eager-validate payload so we don't open an orphan stream when the
        # body would be rejected at append time.
        self._serialize_payload(payload, payload_encoding)
        stream = self.open_stream(
            sender_agent_id,
            recipient_agent_id=recipient_agent_id,
            content_type=content_type,
            topic=topic,
            headers=headers,
            task_id=task_id,
        )
        chunk = self.append_chunk(
            stream.id,
            sender_agent_id,
            payload=payload,
            payload_encoding=payload_encoding,
            final=True,
        )
        self.observability.record_log(
            "agentbus.content.published",
            layer="agentbus",
            source=sender_agent_id,
            subject_type="agentbus_stream",
            subject_id=stream.id,
            detail={
                "sender_agent_id": sender_agent_id,
                "recipient_agent_id": recipient_agent_id,
                "topic": topic,
                "content_type": content_type,
                "payload_encoding": payload_encoding,
                "chunk_id": chunk.id,
            },
        )
        return {
            "stream": self.get_stream(stream.id).to_dict(),
            "chunk": chunk.to_dict(),
        }

    # Validation ---------------------------------------------------------

    def _validate_content_type(self, content_type: str) -> None:
        if not isinstance(content_type, str) or not content_type.strip():
            raise ValidationError("agentbus content_type is required")
        if len(content_type) > 128 or not AGENTBUS_TYPED_CONTENT_RE.match(content_type):
            raise ValidationError("invalid agentbus content_type: %s" % content_type)

    def _validate_topic(self, topic: str) -> str:
        if not isinstance(topic, str) or not topic.strip():
            raise ValidationError("agentbus topic is required")
        topic_value = topic.strip()
        if not AGENTBUS_TOPIC_RE.match(topic_value):
            raise ValidationError("invalid agentbus topic: %s" % topic)
        return topic_value

    def _serialize_payload(self, payload: Any, payload_encoding: str) -> str:
        if payload_encoding not in AGENTBUS_PAYLOAD_ENCODINGS:
            raise ValidationError("unsupported agentbus payload_encoding: %s" % payload_encoding)
        if payload_encoding in {"text", "base64"} and not isinstance(payload, str):
            raise ValidationError("agentbus %s payload must be a string" % payload_encoding)
        if payload_encoding == "base64":
            try:
                base64.b64decode(payload.encode("ascii"), validate=True)
            except Exception as exc:  # noqa: BLE001 - normalize parser errors at API boundary.
                raise ValidationError("agentbus base64 payload is invalid") from exc
        try:
            serialized = json_dumps(payload)
        except (TypeError, ValueError) as exc:
            raise ValidationError("agentbus payload must be JSON serializable") from exc
        if len(serialized.encode("utf-8")) > AGENTBUS_MAX_CHUNK_BYTES:
            raise ValidationError(
                "agentbus chunk exceeds %d-byte limit" % AGENTBUS_MAX_CHUNK_BYTES
            )
        return serialized

    def _authorized(self, stream: AgentBusStream, agent_id: str) -> bool:
        return agent_id in {stream.sender_agent_id, stream.recipient_agent_id}

    # Foreign-key existence checks. The service is the FK enforcement
    # boundary; doing the lookup here avoids a back-reference to ControlPlane.

    def _require_agent(self, agent_id: str) -> None:
        if not self.store.query_one("SELECT id FROM agents WHERE id = ?", (agent_id,)):
            raise NotFoundError("agent not found: %s" % agent_id)

    def _require_task(self, task_id: str) -> None:
        if not self.store.query_one("SELECT id FROM tasks WHERE id = ?", (task_id,)):
            raise NotFoundError("task not found: %s" % task_id)

    # Row hydration ------------------------------------------------------

    def _stream_from_row(self, row: Any) -> AgentBusStream:
        return AgentBusStream(
            row["id"],
            row["sender_agent_id"],
            row["recipient_agent_id"],
            row["task_id"],
            row["topic"],
            row["content_type"],
            json_loads(row["headers"], {}),
            row["status"],
            row["created_at"],
            row["updated_at"],
            row["closed_at"],
        )

    def _chunk_from_row(self, row: Any) -> AgentBusChunk:
        return AgentBusChunk(
            row["id"],
            row["stream_id"],
            int(row["sequence"]),
            row["sender_agent_id"],
            row["content_type"],
            json_loads(row["payload"], None),
            row["payload_encoding"],
            int(row["size_bytes"]),
            row["created_at"],
        )
