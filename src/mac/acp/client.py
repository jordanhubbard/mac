"""``ACPClient`` -- the host/client side of the Agent Client Protocol.

This is the role mac plays when it *drives* an external ACP-compliant agent. It
wraps a :class:`~mac.acp.peer.Peer` and exposes the baseline client-initiated
methods (``initialize``, ``authenticate``, ``session/new``, ``session/prompt``)
as ordinary blocking calls, while letting the caller register handlers for the
two agent-initiated channels:

* ``session/update`` notifications (streaming turn progress) via
  :meth:`on_update`.
* ``session/request_permission`` requests via :meth:`on_request_permission`,
  so mac can later route permission decisions through its own policy/sandbox.

The client is transport-agnostic: it takes a ready :class:`Peer`. Pair it with
:func:`~mac.acp.peer.stdio_peer` for a real subprocess agent, or with an
in-memory paired peer in tests.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .peer import Peer
from .protocol import (
    AuthenticateParams,
    ClientCapabilities,
    InitializeParams,
    InitializeResult,
    Method,
    NewSessionParams,
    NewSessionResult,
    PromptParams,
    PromptResult,
    RequestPermissionParams,
    RequestPermissionResult,
    text_block,
)


__all__ = ["ACPClient", "UpdateHandler", "PermissionHandler"]


#: Receives the raw ``params`` dict of a ``session/update`` notification.
UpdateHandler = Callable[[Dict[str, Any]], None]

#: Receives a parsed :class:`RequestPermissionParams` and returns the client's
#: :class:`RequestPermissionResult` decision.
PermissionHandler = Callable[[RequestPermissionParams], RequestPermissionResult]


class ACPClient:
    """Client-side driver for an ACP agent over a :class:`Peer`."""

    def __init__(
        self,
        peer: Peer,
        *,
        client_capabilities: Optional[ClientCapabilities] = None,
        client_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._peer = peer
        # Default to mac's full capability set (incl. _meta extensions) so a
        # driven agent sees what mac is; an explicit override still wins.
        if client_capabilities is None:
            from .capabilities import mac_client_capabilities

            client_capabilities = mac_client_capabilities()
        self._client_capabilities = client_capabilities
        self._client_info = client_info or {
            "name": "mac",
            "title": "MAC hub",
            "version": "0",
        }
        self._update_handler: Optional[UpdateHandler] = None
        self._permission_handler: Optional[PermissionHandler] = None
        self.agent_capabilities: Optional[InitializeResult] = None

        # Wire the agent-initiated channels into the peer.
        peer.on_notification(Method.SESSION_UPDATE, self._dispatch_update)
        peer.on_request(
            Method.SESSION_REQUEST_PERMISSION, self._dispatch_request_permission
        )

    # -- handler registration ------------------------------------------------

    def on_update(self, handler: UpdateHandler) -> None:
        """Register the sink for ``session/update`` notifications."""

        self._update_handler = handler

    def on_request_permission(self, handler: PermissionHandler) -> None:
        """Register the responder for ``session/request_permission`` requests."""

        self._permission_handler = handler

    # -- agent-initiated dispatch -------------------------------------------

    def _dispatch_update(self, params: Optional[Dict[str, Any]]) -> None:
        if self._update_handler is not None:
            self._update_handler(params or {})

    def _dispatch_request_permission(
        self, params: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        parsed = RequestPermissionParams.from_dict(params or {})
        if self._permission_handler is None:
            # No policy registered -> decline by cancelling, the safe default.
            return RequestPermissionResult(outcome="cancelled").to_dict()
        decision = self._permission_handler(parsed)
        return decision.to_dict()

    # -- baseline client methods --------------------------------------------

    def initialize(
        self, *, timeout: Optional[float] = None
    ) -> InitializeResult:
        """Negotiate the protocol version and capabilities with the agent."""

        params = InitializeParams(
            client_capabilities=self._client_capabilities,
            client_info=self._client_info,
        )
        raw = self._peer.request(Method.INITIALIZE, params.to_dict()).result(timeout)
        result = InitializeResult.from_dict(raw or {})
        self.agent_capabilities = result
        return result

    def authenticate(
        self, method_id: str, *, timeout: Optional[float] = None
    ) -> None:
        """Authenticate using one of the agent-advertised auth methods."""

        params = AuthenticateParams(method_id=method_id)
        self._peer.request(Method.AUTHENTICATE, params.to_dict()).result(timeout)

    def session_new(
        self,
        cwd: str,
        *,
        mcp_servers: Optional[List[Dict[str, Any]]] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Create a new session and return its ``sessionId``."""

        params = NewSessionParams(cwd=cwd, mcp_servers=mcp_servers or [])
        raw = self._peer.request(Method.SESSION_NEW, params.to_dict()).result(timeout)
        return NewSessionResult.from_dict(raw or {}).session_id

    def session_prompt(
        self,
        session_id: str,
        prompt: Any,
        *,
        timeout: Optional[float] = None,
    ) -> PromptResult:
        """Drive a prompt turn; blocks until the agent returns a stop reason.

        ``prompt`` may be a plain string (wrapped into a single ``text`` content
        block) or an explicit list of content-block dicts.
        """

        blocks = self._normalize_prompt(prompt)
        params = PromptParams(session_id=session_id, prompt=blocks)
        raw = self._peer.request(Method.SESSION_PROMPT, params.to_dict()).result(
            timeout
        )
        return PromptResult.from_dict(raw or {})

    def cancel(self, session_id: str) -> None:
        """Send a ``session/cancel`` notification for the given session."""

        self._peer.notify(Method.SESSION_CANCEL, {"sessionId": session_id})

    @staticmethod
    def _normalize_prompt(prompt: Any) -> List[Dict[str, Any]]:
        if isinstance(prompt, str):
            return [text_block(prompt)]
        if isinstance(prompt, dict):
            return [prompt]
        return list(prompt)
