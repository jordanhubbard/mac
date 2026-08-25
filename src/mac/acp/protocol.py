"""Pure data types and (de)serialization for the Agent Client Protocol (ACP).

ACP (https://agentclientprotocol.com) is the open JSON-RPC 2.0 standard for
**host/client <-> agent** communication. This module is the wire layer: it
defines the JSON-RPC 2.0 message envelope, the ACP method-name constants, and
the small set of structured payload types (capabilities, content blocks, stop
reasons). It performs **no I/O** -- everything here is pure data plus
``to_dict`` / ``from_dict`` (de)serialization, so it can be exercised without a
transport.

Spec notes pinned by this implementation (verified against
``agentclientprotocol.com/protocol/v1/*`` on 2026-06-16):

* The protocol version is a single **integer** identifying a MAJOR version
  (currently ``1``) -- *not* a string.
* Property names are ``camelCase``; discriminator string values are
  ``snake_case``.
* ``session/cancel`` is a **notification** (client -> agent), not a request.
* ``session/update`` is a client **notification** (agent -> client).
* ``session/request_permission`` is a client **request** (agent -> client).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Union


__all__ = [
    "PROTOCOL_VERSION",
    "JSONRPC_VERSION",
    "Method",
    "SessionUpdateKind",
    "StopReason",
    "ContentBlockType",
    "PermissionOutcome",
    "JSONRPCError",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCNotification",
    "JSONRPCMessage",
    "decode_message",
    "json_dumps",
    "text_block",
    "ClientCapabilities",
    "AgentCapabilities",
    "AuthMethod",
    "InitializeParams",
    "InitializeResult",
    "AuthenticateParams",
    "NewSessionParams",
    "NewSessionResult",
    "PromptParams",
    "PromptResult",
    "RequestPermissionParams",
    "PermissionOption",
    "RequestPermissionResult",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The ACP MAJOR protocol version this package speaks. A single integer per the
#: spec (``agentclientprotocol.com/protocol/v1/initialization``).
PROTOCOL_VERSION: int = 1

#: JSON-RPC envelope version. Constant for the protocol.
JSONRPC_VERSION: str = "2.0"


class Method:
    """Canonical ACP JSON-RPC method names.

    Grouped by direction/role per the spec. These are plain string constants so
    they round-trip transparently onto the wire's ``method`` field.
    """

    # --- Agent methods (client -> agent requests) ---
    INITIALIZE = "initialize"
    AUTHENTICATE = "authenticate"
    SESSION_NEW = "session/new"
    SESSION_PROMPT = "session/prompt"
    SESSION_LOAD = "session/load"  # optional capability (loadSession)
    SESSION_SET_MODE = "session/set_mode"  # optional
    LOGOUT = "logout"  # optional

    # --- Agent notifications (client -> agent, no response) ---
    SESSION_CANCEL = "session/cancel"

    # --- Client methods (agent -> client requests) ---
    SESSION_REQUEST_PERMISSION = "session/request_permission"
    FS_READ_TEXT_FILE = "fs/read_text_file"  # optional client capability
    FS_WRITE_TEXT_FILE = "fs/write_text_file"  # optional client capability
    TERMINAL_CREATE = "terminal/create"  # optional client capability
    TERMINAL_OUTPUT = "terminal/output"
    TERMINAL_RELEASE = "terminal/release"
    TERMINAL_WAIT_FOR_EXIT = "terminal/wait_for_exit"
    TERMINAL_KILL = "terminal/kill"

    # --- Client notifications (agent -> client, no response) ---
    SESSION_UPDATE = "session/update"


class SessionUpdateKind:
    """Discriminator values for the ``sessionUpdate`` field of a
    ``session/update`` notification payload (snake_case per spec)."""

    AGENT_MESSAGE_CHUNK = "agent_message_chunk"
    AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
    USER_MESSAGE_CHUNK = "user_message_chunk"
    TOOL_CALL = "tool_call"
    TOOL_CALL_UPDATE = "tool_call_update"
    PLAN = "plan"
    AVAILABLE_COMMANDS_UPDATE = "available_commands_update"
    USAGE_UPDATE = "usage_update"


class StopReason:
    """Allowed values for the ``stopReason`` of a ``session/prompt`` result."""

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"


class ContentBlockType:
    """Discriminator values for the ``type`` field of a content block."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    RESOURCE = "resource"
    RESOURCE_LINK = "resource_link"


class PermissionOutcome:
    """Values for the ``outcome.outcome`` field of a permission response."""

    SELECTED = "selected"
    CANCELLED = "cancelled"


# Standard JSON-RPC 2.0 error codes (subset relevant to ACP peers).
ERROR_PARSE = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602
ERROR_INTERNAL = -32603


def json_dumps(value: Any) -> str:
    """Deterministic, compact JSON encoding.

    Mirrors :func:`mac.models.json_dumps` (sorted keys, no whitespace) so ACP
    frames are byte-stable for tests and logging. ACP framing is one JSON object
    per line, so the result must contain no embedded newlines -- ``json.dumps``
    guarantees this for the value types used here.
    """

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 envelope
# ---------------------------------------------------------------------------

# A JSON-RPC id may be a string, number, or null per the spec. ACP peers use
# integers, but we accept any of these on decode.
RpcId = Union[str, int, None]


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

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "JSONRPCError":
        return cls(
            code=int(raw["code"]),
            message=str(raw.get("message", "")),
            data=raw.get("data"),
        )


@dataclass
class JSONRPCRequest:
    """A JSON-RPC 2.0 request (expects a correlated response by ``id``)."""

    id: RpcId
    method: str
    params: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self.id,
            "method": self.method,
        }
        if self.params is not None:
            out["params"] = self.params
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "JSONRPCRequest":
        return cls(
            id=raw.get("id"),
            method=str(raw["method"]),
            params=raw.get("params"),
        )


@dataclass
class JSONRPCNotification:
    """A JSON-RPC 2.0 notification: a method call with no ``id`` and no
    response."""

    method: str
    params: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": self.method}
        if self.params is not None:
            out["params"] = self.params
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "JSONRPCNotification":
        return cls(method=str(raw["method"]), params=raw.get("params"))


@dataclass
class JSONRPCResponse:
    """A JSON-RPC 2.0 response: carries exactly one of ``result`` or ``error``.

    ``result`` of ``None`` is ambiguous (a successful empty result also encodes
    as ``null``), so success is tracked explicitly via :attr:`error`.
    """

    id: RpcId
    result: Any = None
    error: Optional[JSONRPCError] = None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": self.id}
        if self.error is not None:
            out["error"] = self.error.to_dict()
        else:
            # A successful response MUST include ``result`` (may be null).
            out["result"] = self.result
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "JSONRPCResponse":
        err = raw.get("error")
        return cls(
            id=raw.get("id"),
            result=raw.get("result"),
            error=JSONRPCError.from_dict(err) if isinstance(err, Mapping) else None,
        )


JSONRPCMessage = Union[JSONRPCRequest, JSONRPCResponse, JSONRPCNotification]


def decode_message(raw: Union[str, bytes, Mapping[str, Any]]) -> JSONRPCMessage:
    """Decode a single JSON-RPC frame into the appropriate envelope type.

    Accepts a JSON string, bytes, or an already-parsed mapping. The message kind
    is determined structurally per JSON-RPC 2.0:

    * has ``method`` and ``id`` -> :class:`JSONRPCRequest`
    * has ``method`` and no ``id`` -> :class:`JSONRPCNotification`
    * has ``result`` or ``error`` -> :class:`JSONRPCResponse`

    Raises :class:`ValueError` for frames that match none of these shapes.
    """

    if isinstance(raw, (str, bytes)):
        obj = json.loads(raw)
    else:
        obj = raw
    if not isinstance(obj, Mapping):
        raise ValueError("JSON-RPC frame must be a JSON object")

    if "method" in obj:
        if "id" in obj and obj.get("id") is not None:
            return JSONRPCRequest.from_dict(obj)
        # An id of explicit null is still a request per JSON-RPC, but ACP peers
        # treat a missing id as a notification. Disambiguate on key presence.
        if "id" in obj:
            return JSONRPCRequest.from_dict(obj)
        return JSONRPCNotification.from_dict(obj)
    if "result" in obj or "error" in obj:
        return JSONRPCResponse.from_dict(obj)
    raise ValueError("frame is not a valid JSON-RPC request, response, or notification")


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


def text_block(text: str) -> Dict[str, Any]:
    """Build a ``text`` content block, the most common prompt/response unit."""

    return {"type": ContentBlockType.TEXT, "text": text}


# ---------------------------------------------------------------------------
# ACP payload types (the ``params`` / ``result`` bodies of ACP methods)
# ---------------------------------------------------------------------------


@dataclass
class ClientCapabilities:
    """Capabilities the client (mac) advertises in ``initialize``.

    ``fs`` advertises filesystem read/write helpers; ``terminal`` advertises
    terminal support. Both default off -- mac drives agents but does not yet
    offer these client-side helpers in Phase 0/1.

    ``meta`` carries the optional ACP ``_meta`` vendor-extension object (Phase
    3); it is omitted from the wire form when unset, so the baseline shape is
    unchanged. Keys there are additive and ignorable by other implementations.
    """

    fs_read_text_file: bool = False
    fs_write_text_file: bool = False
    terminal: bool = False
    meta: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "fs": {
                "readTextFile": self.fs_read_text_file,
                "writeTextFile": self.fs_write_text_file,
            },
            "terminal": self.terminal,
        }
        if self.meta:
            out["_meta"] = self.meta
        return out

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "ClientCapabilities":
        raw = raw or {}
        fs = raw.get("fs") or {}
        meta = raw.get("_meta")
        return cls(
            fs_read_text_file=bool(fs.get("readTextFile", False)),
            fs_write_text_file=bool(fs.get("writeTextFile", False)),
            terminal=bool(raw.get("terminal", False)),
            meta=dict(meta) if isinstance(meta, Mapping) else None,
        )


@dataclass
class AgentCapabilities:
    """Capabilities an agent advertises in the ``initialize`` result.

    ``meta`` carries the optional ACP ``_meta`` vendor-extension object (Phase
    3); it is omitted from the wire form when unset, so the baseline shape is
    unchanged. Keys there are additive and ignorable by other implementations.
    """

    load_session: bool = False
    prompt_capabilities: Dict[str, Any] = field(default_factory=dict)
    mcp_capabilities: Dict[str, Any] = field(default_factory=dict)
    meta: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"loadSession": self.load_session}
        if self.prompt_capabilities:
            out["promptCapabilities"] = self.prompt_capabilities
        if self.mcp_capabilities:
            out["mcpCapabilities"] = self.mcp_capabilities
        if self.meta:
            out["_meta"] = self.meta
        return out

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "AgentCapabilities":
        raw = raw or {}
        meta = raw.get("_meta")
        return cls(
            load_session=bool(raw.get("loadSession", False)),
            prompt_capabilities=dict(raw.get("promptCapabilities") or {}),
            mcp_capabilities=dict(raw.get("mcpCapabilities") or {}),
            meta=dict(meta) if isinstance(meta, Mapping) else None,
        )


@dataclass
class AuthMethod:
    """An authentication method advertised in the ``initialize`` result."""

    id: str
    name: str
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "name": self.name}
        if self.description is not None:
            out["description"] = self.description
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AuthMethod":
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name", "")),
            description=raw.get("description"),
        )


@dataclass
class InitializeParams:
    """Params for the ``initialize`` request."""

    protocol_version: int = PROTOCOL_VERSION
    client_capabilities: ClientCapabilities = field(default_factory=ClientCapabilities)
    client_info: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "protocolVersion": self.protocol_version,
            "clientCapabilities": self.client_capabilities.to_dict(),
        }
        if self.client_info is not None:
            out["clientInfo"] = self.client_info
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InitializeParams":
        return cls(
            protocol_version=int(raw.get("protocolVersion", PROTOCOL_VERSION)),
            client_capabilities=ClientCapabilities.from_dict(raw.get("clientCapabilities")),
            client_info=raw.get("clientInfo"),
        )


@dataclass
class InitializeResult:
    """Result of the ``initialize`` request (returned by the agent)."""

    protocol_version: int = PROTOCOL_VERSION
    agent_capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    auth_methods: List[AuthMethod] = field(default_factory=list)
    agent_info: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "protocolVersion": self.protocol_version,
            "agentCapabilities": self.agent_capabilities.to_dict(),
            "authMethods": [m.to_dict() for m in self.auth_methods],
        }
        if self.agent_info is not None:
            out["agentInfo"] = self.agent_info
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InitializeResult":
        return cls(
            protocol_version=int(raw.get("protocolVersion", PROTOCOL_VERSION)),
            agent_capabilities=AgentCapabilities.from_dict(raw.get("agentCapabilities")),
            auth_methods=[AuthMethod.from_dict(m) for m in (raw.get("authMethods") or [])],
            agent_info=raw.get("agentInfo"),
        )


@dataclass
class AuthenticateParams:
    """Params for the ``authenticate`` request."""

    method_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {"methodId": self.method_id}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AuthenticateParams":
        return cls(method_id=str(raw["methodId"]))


@dataclass
class NewSessionParams:
    """Params for the ``session/new`` request."""

    cwd: str
    mcp_servers: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"cwd": self.cwd, "mcpServers": list(self.mcp_servers)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NewSessionParams":
        return cls(
            cwd=str(raw.get("cwd", "")),
            mcp_servers=list(raw.get("mcpServers") or []),
        )


@dataclass
class NewSessionResult:
    """Result of ``session/new``."""

    session_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {"sessionId": self.session_id}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NewSessionResult":
        return cls(session_id=str(raw["sessionId"]))


@dataclass
class PromptParams:
    """Params for the ``session/prompt`` request."""

    session_id: str
    prompt: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"sessionId": self.session_id, "prompt": list(self.prompt)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PromptParams":
        return cls(
            session_id=str(raw["sessionId"]),
            prompt=list(raw.get("prompt") or []),
        )


@dataclass
class PromptResult:
    """Result of ``session/prompt`` -- carries the turn's terminating reason."""

    stop_reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"stopReason": self.stop_reason}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PromptResult":
        return cls(stop_reason=str(raw["stopReason"]))


@dataclass
class PermissionOption:
    """An option offered in a ``session/request_permission`` request."""

    option_id: str
    name: str
    kind: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"optionId": self.option_id, "name": self.name}
        if self.kind is not None:
            out["kind"] = self.kind
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PermissionOption":
        return cls(
            option_id=str(raw["optionId"]),
            name=str(raw.get("name", "")),
            kind=raw.get("kind"),
        )


@dataclass
class RequestPermissionParams:
    """Params for the ``session/request_permission`` request (agent -> client)."""

    session_id: str
    tool_call: Dict[str, Any]
    options: List[PermissionOption] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "toolCall": self.tool_call,
            "options": [o.to_dict() for o in self.options],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RequestPermissionParams":
        return cls(
            session_id=str(raw["sessionId"]),
            tool_call=dict(raw.get("toolCall") or {}),
            options=[PermissionOption.from_dict(o) for o in (raw.get("options") or [])],
        )


@dataclass
class RequestPermissionResult:
    """Result of ``session/request_permission`` -- the client's decision.

    The wire shape nests the decision under ``outcome``::

        {"outcome": {"outcome": "selected", "optionId": "allow"}}
        {"outcome": {"outcome": "cancelled"}}
    """

    outcome: str
    option_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        inner: Dict[str, Any] = {"outcome": self.outcome}
        if self.option_id is not None:
            inner["optionId"] = self.option_id
        return {"outcome": inner}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RequestPermissionResult":
        inner = raw.get("outcome") or {}
        return cls(
            outcome=str(inner.get("outcome", PermissionOutcome.CANCELLED)),
            option_id=inner.get("optionId"),
        )
