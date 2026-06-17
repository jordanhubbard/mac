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
"""

from __future__ import annotations

from .client import ACPClient, PermissionHandler, UpdateHandler
from .executor import ACPExecutor, ACPRunResult
from .peer import Peer, PendingRequest, RemoteError, stdio_peer
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
    "ACPClient",
    "UpdateHandler",
    "PermissionHandler",
    "ACPExecutor",
    "ACPRunResult",
]
