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

# The one carve-out from "the bus is not private" (2026-08-17).
#
# Making point-to-point messages fleet-readable was an instruction about agents
# TALKING: coordination traffic — who holds which branch, who is working where.
# OpenShell debug-terminal streams are not that. They carry raw terminal I/O:
# command output, environment, tokens, credentials. They were never in scope of
# that instruction; they merely sat behind the same membership check, which
# conflated two different decisions.
#
# The mistakes are asymmetric: adding these topics to the broadcast later is a
# one-line change, while a credential already read off the bus cannot be
# un-read. So the reversible option wins by default.
#
# TO REMOVE THE CARVE-OUT: empty this set. Nothing else needs to change — it is
# consulted in exactly one decision function (``AgentBusService._may_read``).
# That is the intended path if terminal I/O should broadcast too.
PARTICIPANT_SCOPED_TOPICS = frozenset(
    {
        "mac.debug.terminal.open.v1",
        "mac.debug.terminal.input.v1",
        "mac.debug.terminal.output.v1",
    }
)


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
        """Reading the bus requires being ON the bus, and nothing more.

        This used to return True only for the sender, the recipient, or a
        group participant. It no longer does, and the argument against the old
        rule was already written down two lines below it, for one topic:

            "Human directives are fleet-readable by design: relay-by-citation
             only works if any agent can look a cited directive up and see the
             hub attests its operator origin."

        That exemption existed because a bus where you cannot look up what
        someone cited does not work. The same is true of every other message:
        an agent told "worker-2 already rebased that branch" cannot verify it
        without reading worker-2's stream. The exemption was the general case
        wearing a special case's clothes.

        So membership stops being an access decision. ``recipient_agent_id``
        and ``participants`` remain ADDRESSING: who is being spoken to and who
        is expected to answer. By convention an agent does not answer a
        question until it is addressed by name -- a convention, deliberately
        not enforced here, because the moment it is enforced an agent cannot
        volunteer the one fact that stops another from destroying work.

        Agents are already registered against this hub; whether a caller may
        reach the bus at all is settled by the token, outside this method.

        One exception survives, for streams that are not agents talking at all:
        see ``PARTICIPANT_SCOPED_TOPICS``.
        """
        return self._may_read(
            topic=stream.topic,
            agent_id=agent_id,
            sender_agent_id=stream.sender_agent_id,
            recipient_agent_id=stream.recipient_agent_id,
            participants=stream.participants,
        )

    def _may_read(
        self,
        *,
        topic: Optional[str],
        agent_id: str,
        sender_agent_id: Optional[str],
        recipient_agent_id: Optional[str],
        participants: Optional[List[str]],
    ) -> bool:
        """The single read decision for the bus. Open, except one topic family.

        Every read path routes here so the carve-out cannot drift: widen or
        remove ``PARTICIPANT_SCOPED_TOPICS`` and every reader changes together.
        """
        if topic not in PARTICIPANT_SCOPED_TOPICS:
            return True
        if participants:
            return agent_id in participants
        return agent_id in {sender_agent_id, recipient_agent_id}

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

        Scoped by ADDRESSING -- direct recipient, or a member of a group
        stream. That is not an access rule: any agent may read any stream via
        ``read_chunks``/``read_bus_traffic``. It is what makes an inbox an
        inbox. "Who spoke to me" and "what is being said" are different
        questions, and an inbox that answered the second would wake a working
        agent for every message on the bus.

        Chunks the agent sent itself are excluded: a watcher that woke on its
        own messages would spin.

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
        # the addressing exactly.
        return [
            chunk
            for chunk in chunks
            if self._stream_addresses_agent(agent_id, chunk.stream_id)
        ]

    # Non-blocking inbox consumption (task_7faf8e56).
    #
    # ``read_inbox`` answers "what is addressed to me", but the only consumer
    # the fleet shipped was ``mac admin agentbus wait``, which BLOCKS. That is
    # the right shape for a working agent whose harness can run a watcher in
    # the background, and the wrong shape for an interactive CLI session, which
    # has no background loop to put it in. The observed consequence: two
    # ``mac.repo.update.result.v1`` replies were addressed to a registered CLI
    # session on 2026-08-21 (03:15Z and 03:21Z), opened and closed within
    # ~20ms, and nothing ever surfaced them. The session published to the bus
    # and never received from it.
    #
    # So the inbox gets the other half: ask how much is waiting, and take it,
    # both returning immediately.

    #: Consumer-cursor topic under which an agent's durable inbox position is
    #: kept. One row per agent in ``agentbus_consumer_cursors``, so "already
    #: drained" survives a process restart -- the caller does not have to hold
    #: the cursor itself the way ``wait --after-cursor`` requires.
    INBOX_CURSOR_TOPIC = "agentbus.inbox"

    #: Ceiling on a pending count. The number exists to make a CLI line say
    #: "you have messages", not to be exact at the tail; an unbounded COUNT over
    #: the busiest table on the bus would be the wrong price for that.
    PENDING_INBOX_COUNT_CAP = 500

    def durable_inbox_cursor(self, agent_id: str) -> str:
        """This agent's stored inbox position, or ``""`` if it has never drained."""
        record = self.get_consumer_cursor(agent_id, self.INBOX_CURSOR_TOPIC)
        if not record:
            return ""
        position = record.get("position")
        if isinstance(position, dict):
            return str(position.get("cursor") or "")
        return str(position or "")

    def pending_inbox_count(
        self,
        agent_id: str,
        after_cursor: Optional[str] = None,
        *,
        limit: Optional[int] = None,
    ) -> JsonDict:
        """How many messages are waiting for ``agent_id``, right now.

        Cheap enough to hang off ordinary CLI output (``mac agent show``,
        ``mac task show``): that is the point. An operator who can see "3
        pending" on a command they were already running does not need to know
        the bus exists to discover that someone answered them.

        ``after_cursor`` defaults to the agent's durable position, so the count
        means "new since you last drained" rather than "since the beginning of
        time".
        """
        self._require_agent(agent_id)
        cursor = (
            self.durable_inbox_cursor(agent_id) if after_cursor is None else str(after_cursor)
        )
        cap = max(1, min(int(limit or self.PENDING_INBOX_COUNT_CAP), self.PENDING_INBOX_COUNT_CAP))
        chunks = self.read_inbox(agent_id, cursor, limit=cap)
        return {
            "agent_id": agent_id,
            "count": len(chunks),
            # True when the real backlog may be larger than ``count``. A caller
            # rendering this should say "500+", not "500".
            "capped": len(chunks) >= cap,
            "cursor": cursor,
            "oldest_at": chunks[0].created_at if chunks else None,
            "newest_at": chunks[-1].created_at if chunks else None,
        }

    def drain_inbox(
        self,
        agent_id: str,
        after_cursor: Optional[str] = None,
        *,
        limit: int = 100,
        commit: bool = True,
    ) -> JsonDict:
        """Take everything addressed to ``agent_id`` and advance its position.

        Returns immediately whether or not anything was waiting -- this is the
        surface an interactive session can call between turns, where blocking
        is not an option.

        **Draining does not close the streams it read.** On this bus, closing is
        the SENDER's statement that a conversation is finished, and it is what
        ``agentbus_request`` waits on to collect a reply; a recipient that
        closed an incoming stream would destroy the channel it is supposed to
        answer on. ``close_stream`` enforces this (only the sender may close),
        and this method does not work around it. "Consumed" is expressed the
        way a bus expresses it: the durable read position moves, so the same
        message is not delivered twice.

        ``commit=False`` reads without advancing -- a peek, for a caller that
        wants to look before it can promise to act.
        """
        self._require_agent(agent_id)
        cursor = (
            self.durable_inbox_cursor(agent_id) if after_cursor is None else str(after_cursor)
        )
        chunks = self.read_inbox(agent_id, cursor, limit=limit)
        messages: List[JsonDict] = []
        next_cursor = cursor
        for chunk in chunks:
            next_cursor = self.inbox_cursor(chunk)
            entry = chunk.to_dict()
            entry["inbox_cursor"] = next_cursor
            messages.append(entry)
        committed = False
        if commit and chunks:
            # Compare-and-set prevents two concurrent drains from both
            # consuming the same batch, and prevents an explicit stale cursor
            # from moving the durable position backwards.
            now = utcnow()
            encoded = json_dumps({"cursor": next_cursor, "updated_at": now})
            with self.store.transaction() as conn:
                current = conn.execute(
                    "SELECT position FROM agentbus_consumer_cursors "
                    "WHERE agent_id = ? AND topic = ?",
                    (agent_id, self.INBOX_CURSOR_TOPIC),
                ).fetchone()
                current_cursor = ""
                if current is not None:
                    position = json_loads(current["position"], {})
                    current_cursor = str(
                        position.get("cursor") if isinstance(position, dict) else position
                    )
                if current_cursor != cursor:
                    return {
                        "agent_id": agent_id,
                        "count": 0,
                        "cursor": current_cursor,
                        "next_cursor": current_cursor,
                        "committed": False,
                        "conflict": True,
                        "messages": [],
                    }
                conn.execute(
                    "INSERT INTO agentbus_consumer_cursors "
                    "(agent_id, topic, position, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(agent_id, topic) DO UPDATE SET "
                    "position = excluded.position, updated_at = excluded.updated_at",
                    (agent_id, self.INBOX_CURSOR_TOPIC, encoded, now),
                )
                committed = True
        # Agent health tracks an UNCONSUMED control stream (see
        # ``_agent_unconsumed_control_stream_age_from_row``). Stamping on every
        # drain would clear that signal whenever an agent read any unrelated
        # chatter, so only a genuine control stream counts as consumed.
        if any(
            is_control_stream(stream.topic, stream.content_type)
            for stream in self._streams_for_chunks(chunks)
        ):
            self._stamp_control_stream_consumed(agent_id)
        return {
            "agent_id": agent_id,
            "count": len(messages),
            "cursor": cursor,
            "next_cursor": next_cursor,
            "committed": committed,
            "messages": messages,
        }

    def _streams_for_chunks(self, chunks: List[AgentBusChunk]) -> List[AgentBusStream]:
        """Hydrate the distinct streams behind ``chunks``, skipping any that vanished."""
        streams: List[AgentBusStream] = []
        for stream_id in dict.fromkeys(chunk.stream_id for chunk in chunks):
            try:
                streams.append(self.get_stream(stream_id))
            except NotFoundError:
                continue
        return streams

    def read_bus_traffic(
        self,
        agent_id: str,
        after_cursor: str = "",
        limit: int = 100,
        *,
        include_addressed: bool = True,
    ) -> List[JsonDict]:
        """Everything being said on the bus, as ``agent_id`` hears it.

        ``read_inbox`` answers "has anyone said anything to me". This answers
        the other question — "what is the fleet saying" — and it is the whole
        point of a bus: an agent about to touch a branch can hear that a peer
        already has it.

        Point-to-point messages are NOT private. ``recipient_agent_id`` and
        ``participants`` are addressing, not access: they say who is being
        spoken to and, by convention, who is expected to answer. Each entry
        carries ``addressed_to`` and ``addressed_to_me`` so a consumer can
        honour that convention — an agent does not answer a question until it
        is addressed by name — without the hub enforcing it. Enforcement would
        be worse than the convention: it would stop an agent from volunteering
        the one fact that keeps another from destroying work.

        Chunks the agent sent itself are always excluded; a watcher woken by
        its own writes would spin. ``include_addressed=False`` drops the ones
        already in its inbox, for a consumer that handles those separately.
        """
        self._require_agent(agent_id)
        cursor_at, separator, cursor_id = str(after_cursor or "").partition(
            self.INBOX_CURSOR_SEPARATOR
        )
        if not (separator and cursor_at and cursor_id and cursor_at[:1].isdigit()):
            cursor_at = ""
        params: List[Any] = [agent_id]
        cursor_clause = ""
        if cursor_at:
            cursor_clause = "AND (c.created_at, c.id) > (?, ?)"
            params.extend([cursor_at, cursor_id])
        params.append(max(1, min(int(limit), 500)))
        rows = self.store.query_all(
            """
            SELECT c.*, s.sender_agent_id AS stream_sender_agent_id,
                   s.recipient_agent_id AS stream_recipient_agent_id,
                   s.topic AS stream_topic, s.participants AS stream_participants
            FROM agentbus_chunks c
            JOIN agentbus_streams s ON s.id = c.stream_id
            WHERE c.sender_agent_id <> ?
              %s
            ORDER BY c.created_at, c.id
            LIMIT ?
            """
            % cursor_clause,
            tuple(params),
        )
        traffic: List[JsonDict] = []
        for row in rows:
            participants = json_loads(row["stream_participants"], None) or []
            sender = row["stream_sender_agent_id"]
            # Same single decision function the per-stream read uses, so the
            # participant-scoped carve-out cannot be true on one path and
            # false on another.
            if not self._may_read(
                topic=row["stream_topic"],
                agent_id=agent_id,
                sender_agent_id=sender,
                recipient_agent_id=row["stream_recipient_agent_id"],
                participants=participants,
            ):
                continue
            addressed = sorted(
                item
                for item in {row["stream_recipient_agent_id"], *participants}
                if item and item != sender
            )
            addressed_to_me = agent_id in addressed
            if addressed_to_me and not include_addressed:
                continue
            chunk = self._chunk_from_row(row)
            traffic.append(
                {
                    "chunk": chunk.to_dict(),
                    "cursor": self.inbox_cursor(chunk),
                    "topic": row["stream_topic"],
                    "from_agent_id": sender,
                    # Addressing, not access: who was spoken to, and hence who
                    # is expected to answer by convention.
                    "addressed_to": addressed,
                    "addressed_to_me": addressed_to_me,
                    "reply_expected": addressed_to_me,
                }
            )
        return traffic

    def _stream_addresses_agent(self, agent_id: str, stream_id: str) -> bool:
        """Is ``agent_id`` spoken TO on this stream? Addressing, not access.

        Every agent may read every stream (see ``_authorized``). This answers
        the narrower routing question the inbox is built on.
        """
        try:
            stream = self.get_stream(stream_id)
        except Exception:  # noqa: BLE001 - a vanished stream is simply gone
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
