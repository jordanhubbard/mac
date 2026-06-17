"""Agent Client Protocol (ACP) support for mac.

ACP (https://agentclientprotocol.com) is the open JSON-RPC 2.0 standard for
host/client <-> agent communication -- the role LSP plays for editors. This
package realizes ADR 0006 (Phase 0 + Phase-1 core):

* :mod:`mac.acp.protocol` -- pure wire types, method constants, PROTOCOL_VERSION.
* :mod:`mac.acp.peer` -- a transport-agnostic, synchronously-drivable JSON-RPC
  2.0 peer plus a thin stdio binding.
* :mod:`mac.acp.client` -- :class:`ACPClient`, the host side that drives an
  external ACP agent.
* :mod:`mac.acp.executor` -- :class:`ACPExecutor`, the standalone adapter the
  task executor will sit behind under ``MAC_EXECUTOR_BACKEND=acp`` (integration
  is a deliberate Phase-1 follow-up; not wired here).
* :mod:`mac.acp.server` -- :class:`ACPAgentServer`, the agent/server side that
  lets an external ACP client drive a mac agent. A prompt turn is handed to a
  :class:`PromptBackend` (the seam mac's task/tool execution will plug into; the
  production backend is the Phase-2 follow-up).
"""

from __future__ import annotations

from .capabilities import (
    MAC_EXTENSIONS,
    acp_manifest,
    mac_agent_capabilities,
    mac_client_capabilities,
    mac_meta,
)
from .backend import MacAgentBackend
from .client import ACPClient, PermissionHandler, UpdateHandler
from .executor import ACPExecutor, ACPRunResult
from .permission import (
    PermissionDecision,
    PermissionMode,
    evaluate_permission,
    load_openshell_policy,
    permission_mode,
)
from .peer import DEFERRED, Peer, PendingRequest, RemoteError, stdio_peer
from .ws_client import ACPWebSocketClient, connect_acp_websocket
from .server import (
    ACPAgentServer,
    EchoBackend,
    PromptBackend,
    PromptBackendFn,
    PromptTurn,
    serve_stdio,
)
from .protocol import (
    PROTOCOL_VERSION,
    AgentCapabilities,
    AuthMethod,
    ClientCapabilities,
    ContentBlockType,
    InitializeParams,
    InitializeResult,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    Method,
    NewSessionParams,
    NewSessionResult,
    PermissionOption,
    PermissionOutcome,
    PromptParams,
    PromptResult,
    RequestPermissionParams,
    RequestPermissionResult,
    SessionUpdateKind,
    StopReason,
    decode_message,
    text_block,
)


__all__ = [
    "PROTOCOL_VERSION",
    "MAC_EXTENSIONS",
    "acp_manifest",
    "mac_agent_capabilities",
    "mac_client_capabilities",
    "mac_meta",
    "PermissionDecision",
    "PermissionMode",
    "evaluate_permission",
    "load_openshell_policy",
    "permission_mode",
    "Method",
    "SessionUpdateKind",
    "StopReason",
    "ContentBlockType",
    "PermissionOutcome",
    "JSONRPCError",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCNotification",
    "decode_message",
    "text_block",
    "ClientCapabilities",
    "AgentCapabilities",
    "AuthMethod",
    "InitializeParams",
    "InitializeResult",
    "NewSessionParams",
    "NewSessionResult",
    "PromptParams",
    "PromptResult",
    "PermissionOption",
    "RequestPermissionParams",
    "RequestPermissionResult",
    "Peer",
    "PendingRequest",
    "RemoteError",
    "stdio_peer",
    "DEFERRED",
    "ACPClient",
    "UpdateHandler",
    "PermissionHandler",
    "ACPExecutor",
    "ACPRunResult",
    "connect_acp_websocket",
    "ACPWebSocketClient",
    "ACPAgentServer",
    "PromptTurn",
    "PromptBackend",
    "PromptBackendFn",
    "EchoBackend",
    "MacAgentBackend",
    "serve_stdio",
]
