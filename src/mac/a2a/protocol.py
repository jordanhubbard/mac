"""Pure data types and (de)serialization for inbound A2A (Agent2Agent).

A2A (https://a2a-protocol.org, Linux Foundation; absorbed IBM's "Agent
Communication Protocol") is the open JSON-RPC 2.0 standard for **agent <->
agent** delegation. This module is the wire layer for mac's inbound A2A
server: it defines the JSON-RPC 2.0 envelope, the A2A method-name constants,
the ``TaskState`` constants, and the structured ``Message`` / ``Part`` /
``Task`` payload types. It performs **no I/O** -- everything here is pure data
plus ``to_dict`` / ``from_dict`` (de)serialization, so it can be exercised
without a transport.

This is the agent<->agent axis and is distinct from :mod:`mac.acp.protocol`,
which is the host<->agent runtime seam. Both speak JSON-RPC 2.0, so the
envelope mirrors ACP's, but the method names and payload shapes are A2A's.

Spec notes pinned by this implementation (verified against
``a2a-protocol.org`` on 2026-06-17):

* JSON-RPC method names are slash-namespaced: ``message/send``,
  ``message/stream`` (deferred), ``tasks/get``, ``tasks/cancel``.
* A ``Part`` in the JSON binding is discriminated by a ``kind`` field; a text
  part is ``{"kind": "text", "text": ...}``. (The gRPC/protobuf binding uses a
  ``OneOf`` instead -- not used here.)
* ``TaskState`` JSON values are lowercase, hyphenated: ``submitted`` /
  ``working`` / ``input-required`` / ``completed`` / ``canceled`` (one ``l``,
  US spelling) / ``failed`` / ``rejected`` / ``auth-required``.
* ``Task.status`` is a ``TaskStatus`` object: ``{"state", "timestamp",
  "message"?}``.
* ``Message`` carries ``role`` ("user" / "agent"), ``parts``, and
  ``messageId``; ``contextId`` / ``taskId`` are optional.

Property names are ``camelCase`` to match the wire.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Union


__all__ = [
    "JSONRPC_VERSION",
    "Method",
    "PartKind",
    "Role",
    "TaskState",
    "ERROR_PARSE",
    "ERROR_INVALID_REQUEST",
    "ERROR_METHOD_NOT_FOUND",
    "ERROR_INVALID_PARAMS",
    "ERROR_INTERNAL",
    "ERROR_TASK_NOT_FOUND",
    "JSONRPCError",
    "json_dumps",
    "new_message_id",
    "text_part",
    "text_from_parts",
    "Message",
    "TaskStatus",
    "Task",
    "rpc_result",
    "rpc_error",
]


#: JSON-RPC envelope version. Constant for the protocol.
JSONRPC_VERSION: str = "2.0"


class Method:
    """Canonical A2A JSON-RPC method names (slash-namespaced per the spec)."""

    MESSAGE_SEND = "message/send"
    MESSAGE_STREAM = "message/stream"  # deferred (SSE) -- declared, not served
    TASKS_GET = "tasks/get"
    TASKS_CANCEL = "tasks/cancel"


class PartKind:
    """Discriminator values for the ``kind`` field of a message ``Part``."""

    TEXT = "text"
    FILE = "file"
    DATA = "data"


class Role:
    """A2A message roles. The remote peer is the ``user``; mac is the ``agent``."""

    USER = "user"
    AGENT = "agent"


class TaskState:
    """A2A ``TaskState`` JSON values (lowercase, hyphenated per the spec)."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    CANCELED = "canceled"  # US spelling, single 'l', per the A2A spec
    FAILED = "failed"
    REJECTED = "rejected"
    AUTH_REQUIRED = "auth-required"


# Standard JSON-RPC 2.0 error codes plus an A2A-specific task-not-found code.
ERROR_PARSE = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602
ERROR_INTERNAL = -32603
#: A2A reserves -32001 for "task not found" (TaskNotFoundError).
ERROR_TASK_NOT_FOUND = -32001


def json_dumps(value: Any) -> str:
    """Deterministic, compact JSON encoding.

    Mirrors :func:`mac.models.json_dumps` and :func:`mac.acp.protocol.json_dumps`
    (sorted keys, no whitespace) so A2A frames are byte-stable for tests and
    logging.
    """

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def new_message_id() -> str:
    """A fresh ``messageId`` for an agent-authored A2A message."""

    return "msg-" + uuid.uuid4().hex


def text_part(text: str) -> Dict[str, Any]:
    """Build a text ``Part`` -- ``{"kind": "text", "text": ...}``."""

    return {"kind": PartKind.TEXT, "text": text}


def text_from_parts(parts: Any) -> str:
    """Concatenate the text of all text-kind parts in ``parts``.

    Tolerant of the two part encodings seen in the wild: the JSON-RPC binding's
    ``{"kind": "text", "text": ...}`` and the looser ``{"type": "text", ...}``
    some clients still emit. Non-text parts are skipped. Returns ``""`` when no
    text is present (the caller decides whether that is an error).
    """

    if not isinstance(parts, (list, tuple)):
        return ""
    chunks: List[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        kind = part.get("kind") or part.get("type")
        if kind not in (None, PartKind.TEXT):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks)


@dataclass
class JSONRPCError:
    """The ``error`` object of a JSON-RPC 2.0 failure response."""

    code: int
    message: str
    data: Any = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


@dataclass
class Message:
    """An A2A ``Message`` -- a turn from a peer (``user``) or mac (``agent``).

    ``parts`` are raw dicts (each a text/file/data part); :func:`text_part`
    builds the common text case and :func:`text_from_parts` extracts it.
    """

    role: str
    parts: List[Dict[str, Any]] = field(default_factory=list)
    message_id: str = field(default_factory=new_message_id)
    context_id: Optional[str] = None
    task_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def text(self) -> str:
        return text_from_parts(self.parts)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "role": self.role,
            "parts": list(self.parts),
            "messageId": self.message_id,
            "kind": "message",
        }
        if self.context_id is not None:
            out["contextId"] = self.context_id
        if self.task_id is not None:
            out["taskId"] = self.task_id
        if self.metadata is not None:
            out["metadata"] = self.metadata
        return out

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "Message":
        raw = raw or {}
        parts = raw.get("parts")
        meta = raw.get("metadata")
        return cls(
            role=str(raw.get("role", Role.USER)),
            parts=[dict(p) for p in parts if isinstance(p, Mapping)]
            if isinstance(parts, (list, tuple))
            else [],
            message_id=str(raw.get("messageId") or new_message_id()),
            context_id=raw.get("contextId"),
            task_id=raw.get("taskId"),
            metadata=dict(meta) if isinstance(meta, Mapping) else None,
        )


@dataclass
class TaskStatus:
    """An A2A ``TaskStatus`` -- the current lifecycle ``state`` + ``timestamp``.

    ``message`` is an optional agent ``Message`` providing context for the
    state (e.g. why it is ``input-required``); omitted from the wire when unset.
    """

    state: str
    timestamp: Optional[str] = None
    message: Optional[Message] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"state": self.state}
        if self.timestamp is not None:
            out["timestamp"] = self.timestamp
        if self.message is not None:
            out["message"] = self.message.to_dict()
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskStatus":
        msg = raw.get("message")
        return cls(
            state=str(raw["state"]),
            timestamp=raw.get("timestamp"),
            message=Message.from_dict(msg) if isinstance(msg, Mapping) else None,
        )


@dataclass
class Task:
    """An A2A ``Task`` -- the unit of delegated work tracked across turns.

    ``id`` is the task's stable identifier (mac binds it to the ledger task id);
    ``contextId`` groups related tasks for one logical interaction. ``history``
    is the ordered list of messages exchanged about the task.
    """

    id: str
    context_id: str
    status: TaskStatus
    history: List[Message] = field(default_factory=list)
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "contextId": self.context_id,
            "status": self.status.to_dict(),
            "history": [m.to_dict() for m in self.history],
            "kind": "task",
        }
        if self.metadata is not None:
            out["metadata"] = self.metadata
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Task":
        history = raw.get("history")
        meta = raw.get("metadata")
        return cls(
            id=str(raw["id"]),
            context_id=str(raw.get("contextId", "")),
            status=TaskStatus.from_dict(raw.get("status") or {"state": TaskState.SUBMITTED}),
            history=[Message.from_dict(m) for m in history if isinstance(m, Mapping)]
            if isinstance(history, (list, tuple))
            else [],
            metadata=dict(meta) if isinstance(meta, Mapping) else None,
        )


# JSON-RPC id may be a string, number, or null per the spec.
RpcId = Union[str, int, None]


def rpc_result(rpc_id: RpcId, result: Any) -> Dict[str, Any]:
    """Build a JSON-RPC 2.0 success response envelope."""

    return {"jsonrpc": JSONRPC_VERSION, "id": rpc_id, "result": result}


def rpc_error(
    rpc_id: RpcId, code: int, message: str, data: Any = None
) -> Dict[str, Any]:
    """Build a JSON-RPC 2.0 error response envelope."""

    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": rpc_id,
        "error": JSONRPCError(code=code, message=message, data=data).to_dict(),
    }
