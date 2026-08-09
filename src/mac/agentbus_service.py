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

from mac.agentbus_control import is_control_stream
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

# Human directives (the hub-verified human->agent channel): the API layer
# refuses agent-bound tokens publishing to this topic, so a stream carrying
# it is PROOF of operator origin — authority as attested provenance, not
# message content. Directives are fleet-readable so a peer can relay one by
# citing its stream id and any receiver can verify at the hub.
HUMAN_DIRECTIVE_TOPIC = "human.directive.v1"
HUMAN_DIRECTIVE_CONTENT_TYPE = "application/vnd.mac.human-directive+json"
HUMAN_DIRECTIVE_SCHEMA = "mac.human.directive.v1"


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
        participant_agent_ids: Optional[List[str]] = None,
    ) -> AgentBusStream:
        self._require_agent(sender_agent_id)
        # Group streams (task_588b67fd): a member list makes this one shared
        # conversation — membership governs reads AND appends. The opener is
        # always a member; recipient_agent_id is kept as the first recipient
        # for compatibility with pair-shaped consumers.
        participants: Optional[List[str]] = None
        if participant_agent_ids:
            members = [
                str(item).strip()
                for item in participant_agent_ids
                if str(item or "").strip()
            ]
            ordered = list(dict.fromkeys([sender_agent_id, *members]))
            if len(ordered) < 2:
                raise ValidationError(
                    "agentbus group stream requires at least one participant "
                    "besides the sender"
                )
            if len(ordered) > 32:
                raise ValidationError("agentbus group stream capped at 32 participants")
            for member in ordered:
                self._require_agent(member)
            participants = ordered
            recipient_agent_id = recipient_agent_id or next(
                member for member in ordered if member != sender_agent_id
            )
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
                content_type, headers, status, created_at, updated_at,
                closed_at, participants
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
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
                json_dumps(participants) if participants else None,
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
                **({"participants": participants} if participants else {}),
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
        # Contract layer (task_0d50e190): payloads declaring a REGISTERED
        # schema are validated at publish time so producers learn about
        # malformed messages immediately, instead of consumers discovering
        # them at turn time. Unregistered schema names stay advisory.
        from mac.agentbus_schemas import is_registered, validate_payload

        declared_schema, problems = validate_payload(payload)
        if problems:
            raise ValidationError(
                "agentbus payload violates %s: %s"
                % (declared_schema, "; ".join(problems))
            )
        if declared_schema and not is_registered(declared_schema):
            self.observability.record_log(
                "agentbus.schema.unregistered",
                layer="agentbus",
                source=sender_agent_id,
                level="warning",
                subject_type="agentbus_stream",
                subject_id=stream_id,
                detail={"schema": declared_schema},
            )
        chunk_id = new_id("chunk")
        now = utcnow()
        with self.store.transaction() as conn:
            stream_row = conn.execute(
                "SELECT * FROM agentbus_streams WHERE id = ?",
                (stream_id,),
            ).fetchone()
            if stream_row is None:
                raise NotFoundError("agentbus stream not found: %s" % stream_id)
            keys = stream_row.keys() if hasattr(stream_row, "keys") else []
            group_members = (
                json_loads(stream_row["participants"], None)
                if "participants" in keys and stream_row["participants"]
                else None
            )
            if group_members:
                # Group thread: any member may append (task_588b67fd).
                if sender_agent_id not in group_members:
                    raise AuthorizationError(
                        "only group participants can append chunks"
                    )
            elif stream_row["sender_agent_id"] != sender_agent_id:
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
        if final:
            finalized_stream = self.get_stream(stream_id)
            if finalized_stream.recipient_agent_id and is_control_stream(
                finalized_stream.topic, finalized_stream.content_type
            ):
                self._stamp_control_stream_published(
                    finalized_stream.recipient_agent_id
                )
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
            # Membership in a group stream is stored as a JSON array; the
            # quoted-id LIKE is exact because agent ids contain no quotes.
            clauses.append(
                "(sender_agent_id = ? OR recipient_agent_id = ? "
                "OR participants LIKE ?)"
            )
            params.extend([agent_id, agent_id, '%"' + agent_id + '"%'])
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
        if agent_id is not None and any(
            row["recipient_agent_id"] == agent_id
            and is_control_stream(row["topic"], row["content_type"])
            for row in rows
        ):
            self._stamp_control_stream_consumed(agent_id)
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
        *,
        record_observation: bool = True,
    ) -> List[AgentBusChunk]:
        stream = self.assert_authorized(agent_id, stream_id)
        if (
            stream.recipient_agent_id == agent_id
            and is_control_stream(stream.topic, stream.content_type)
        ):
            self._stamp_control_stream_consumed(agent_id)
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
        if chunks and record_observation:
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
        participant_agent_ids: Optional[List[str]] = None,
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
            participant_agent_ids=participant_agent_ids,
        )
        # Pair publishes are one-shot (chunk finalizes the stream). A group
        # publish is the OPENING of a conversation: members reply as further
        # chunks on this same stream, so it stays open until the opener
        # closes it.
        chunk = self.append_chunk(
            stream.id,
            sender_agent_id,
            payload=payload,
            payload_encoding=payload_encoding,
            final=not bool(participant_agent_ids),
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

    def _stamp_control_stream_published(self, agent_id: str) -> None:
        now = utcnow()
        self.store.execute(
            """
            UPDATE agents
            SET last_control_stream_published_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, agent_id),
        )

    def _stamp_control_stream_consumed(self, agent_id: str) -> None:
        now = utcnow()
        self.store.execute(
            """
            UPDATE agents
            SET last_control_stream_consumed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, agent_id),
        )

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

    # Consumer cursors (task_0d50e190): hub-durable read positions so a
    # gateway rebuild no longer loses its place (the peer bridge previously
    # kept its bookmark in a sandbox-local file). The position document is
    # opaque to the hub — client-defined semantics, bounded size.

    CURSOR_MAX_BYTES = 8192

    def set_consumer_cursor(
        self,
        agent_id: str,
        topic: str,
        position: Any,
    ) -> JsonDict:
        self._require_agent(agent_id)
        topic_value = self._validate_topic(topic)
        encoded = json_dumps(position)
        if len(encoded.encode("utf-8")) > self.CURSOR_MAX_BYTES:
            raise ValidationError(
                "agentbus cursor position exceeds %d bytes" % self.CURSOR_MAX_BYTES
            )
        now = utcnow()
        self.store.execute(
            """
            INSERT INTO agentbus_consumer_cursors (agent_id, topic, position, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(agent_id, topic) DO UPDATE SET
                position = excluded.position,
                updated_at = excluded.updated_at
            """,
            (agent_id, topic_value, encoded, now),
        )
        return {
            "agent_id": agent_id,
            "topic": topic_value,
            "position": position,
            "updated_at": now,
        }

    def get_consumer_cursor(self, agent_id: str, topic: str) -> Optional[JsonDict]:
        self._require_agent(agent_id)
        topic_value = self._validate_topic(topic)
        row = self.store.query_one(
            "SELECT * FROM agentbus_consumer_cursors WHERE agent_id = ? AND topic = ?",
            (agent_id, topic_value),
        )
        if row is None:
            return None
        return {
            "agent_id": agent_id,
            "topic": topic_value,
            "position": json_loads(row["position"], None),
            "updated_at": row["updated_at"],
        }

    def _authorized(self, stream: AgentBusStream, agent_id: str) -> bool:
        # Human directives are fleet-readable by design: relay-by-citation
        # only works if any agent can look a cited directive up and see the
        # hub attests its operator origin.
        if stream.topic == HUMAN_DIRECTIVE_TOPIC:
            return True
        if stream.participants:
            return agent_id in stream.participants
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
        keys = row.keys() if hasattr(row, "keys") else []
        participants = (
            json_loads(row["participants"], None)
            if "participants" in keys and row["participants"]
            else None
        )
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
            participants,
        )

    #: Opaque inbox cursor. agentbus_chunks has no global monotonic key -- only
    #: UNIQUE(stream_id, sequence) -- so a cross-stream cursor is the (created_at,
    #: id) pair, which is total and stable. Callers treat it as opaque.
    INBOX_CURSOR_SEPARATOR = "|"

    @classmethod
    def inbox_cursor(cls, chunk: AgentBusChunk) -> str:
        """The opaque resume cursor for ``chunk``.

        Validates rather than dereferencing blindly: this is reachable from the
        CLI and the control-plane surface, where a caller can hand it anything,
        and a public control-plane method owes a domain error rather than an
        AttributeError.
        """
        created_at = getattr(chunk, "created_at", None)
        chunk_id = getattr(chunk, "id", None)
        if not created_at or not chunk_id:
            raise ValidationError(
                "agentbus inbox cursor requires a chunk with created_at and id"
            )
        return "%s%s%s" % (created_at, cls.INBOX_CURSOR_SEPARATOR, chunk_id)

    def read_inbox(
        self,
        agent_id: str,
        after_cursor: str = "",
        limit: int = 100,
    ) -> List[AgentBusChunk]:
        """Messages addressed to ``agent_id`` across every stream it can see.

        The per-stream reader (`read_chunks`) answers "what is new in this
        conversation", which requires already knowing the stream. An agent that
        is *working* does not know which stream a correction will arrive on, so
        this answers the other question: "has anyone said anything to me".

        Membership is the same rule the bus already enforces -- direct recipient,
        or a member of a group stream. Chunks the agent sent itself are excluded:
        a watcher that woke on its own messages would spin.

        Ordering is (created_at, id) because agentbus_chunks has no global
        sequence; `sequence` is per-stream and would interleave incorrectly
        across conversations.
        """
        cursor_at, separator, cursor_id = str(after_cursor or "").partition(
            self.INBOX_CURSOR_SEPARATOR
        )
        # A malformed cursor must OVER-deliver, never under-deliver: a watcher
        # restarted with a corrupted value that silently filtered everything out
        # would miss exactly the correction it exists to catch. "not-a-cursor"
        # has no separator, and letters sort above digits, so treating it as a
        # timestamp bound hid the whole inbox. Anything that is not a
        # `<created_at>|<id>` pair beginning with a digit is ignored.
        if not (separator and cursor_at and cursor_id and cursor_at[:1].isdigit()):
            cursor_at = ""
        params: List[Any] = [agent_id, agent_id, agent_id]
        cursor_clause = ""
        if cursor_at:
            # Row-value comparison keeps the pair total; a plain created_at > ?
            # would drop chunks sharing a timestamp.
            cursor_clause = "AND (c.created_at, c.id) > (?, ?)"
            params.extend([cursor_at, cursor_id])
        params.append(max(1, min(int(limit), 500)))
        rows = self.store.query_all(
            """
            SELECT c.* FROM agentbus_chunks c
            JOIN agentbus_streams s ON s.id = c.stream_id
            WHERE (
                s.recipient_agent_id = ?
                OR (s.participants IS NOT NULL AND s.participants LIKE '%%' || ? || '%%')
            )
              AND c.sender_agent_id <> ?
              %s
            ORDER BY c.created_at, c.id
            LIMIT ?
            """
            % cursor_clause,
            tuple(params),
        )
        chunks = [self._chunk_from_row(row) for row in rows]
        # A LIKE on the participants JSON is a cheap prefilter, not the check --
        # it would match an agent id that is a substring of another. Confirm
        # membership exactly.
        return [
            chunk
            for chunk in chunks
            if self._agent_may_see_stream(agent_id, chunk.stream_id)
        ]

    def _agent_may_see_stream(self, agent_id: str, stream_id: str) -> bool:
        try:
            stream = self.get_stream(stream_id)
        except Exception:  # noqa: BLE001 - a vanished stream is simply not visible
            return False
        if stream.recipient_agent_id == agent_id:
            return True
        participants = stream.participants or []
        return agent_id in participants

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
