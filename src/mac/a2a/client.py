"""Outbound A2A client: delegate work to *external* A2A agents.

This is the client side of the same JSON-RPC 2.0 protocol mac serves inbound
(:mod:`mac.a2a.service`). Where the service maps incoming A2A RPCs onto mac's
task ledger, this module lets a mac agent **discover** a remote A2A agent via
its AgentCard and **delegate** work to it: send a message, then poll / cancel
the resulting :class:`~mac.a2a.protocol.Task`.

A2A (https://a2a-protocol.org, Linux Foundation; absorbed IBM's "Agent
Communication Protocol") is the open agent<->agent standard. The wire types
(JSON-RPC envelope, ``Message`` / ``Part`` / ``Task`` / ``TaskState``, the
method-name constants) live in :mod:`mac.a2a.protocol` and are **reused** here
verbatim -- this module adds only the transport and request-building.

Transport is an injectable seam (:class:`A2AClient`'s ``transport`` arg) so the
client can be exercised in-process against the real inbound server with no
socket. The default :class:`_UrllibTransport` is a thin stdlib ``urllib``
implementation: it POSTs JSON-RPC frames to ``{base_url}/a2a`` and GETs the
AgentCard, adding ``Authorization: Bearer {token}`` when a token is set.

Spec notes pinned by this implementation (matching the inbound side):

* JSON-RPC method names are slash-namespaced (``message/send`` / ``tasks/get``
  / ``tasks/cancel``); the wire is JSON-RPC 2.0.
* ``message/send`` params carry a single ``message`` object (role ``user``,
  one text ``Part``) plus a fresh ``messageId``; the result is a ``Task``.
* The canonical discovery path is ``/.well-known/agent-card.json`` (A2A v0.3+),
  with ``/.well-known/agent.json`` as the pre-v0.3 legacy fallback.

Deferred (out of scope, mirroring the inbound side): ``message/stream`` SSE
consumption, ``tasks/resubscribe``, and push notifications. A caller drives
long-running delegated work by polling :meth:`A2AClient.get_task`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping, Optional, Protocol

from .card import WELL_KNOWN_CARD_PATH, WELL_KNOWN_CARD_PATH_LEGACY
from .protocol import (
    JSONRPC_VERSION,
    Message,
    Method,
    Role,
    Task,
    json_dumps,
    new_message_id,
    text_part,
)


__all__ = ["A2AClient", "A2AClientError", "Transport"]


class A2AClientError(Exception):
    """A JSON-RPC error returned by a remote A2A agent.

    Carries the JSON-RPC ``code`` and ``message`` (and optional ``data``) so a
    caller can branch on, e.g., task-not-found (-32001) vs invalid-params
    (-32602) without re-parsing the envelope.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__("A2A error %s: %s" % (code, message))
        self.code = code
        self.message = message
        self.data = data


class Transport(Protocol):
    """The transport seam :class:`A2AClient` drives.

    An implementation does plain request/response over whatever wire it likes;
    the client supplies fully-formed JSON-RPC bodies and parses the returned
    dicts. The default is :class:`_UrllibTransport`; tests inject an in-process
    fake that routes straight to the inbound server.
    """

    def post(self, path: str, json_body: Mapping[str, Any]) -> Dict[str, Any]:
        """POST ``json_body`` to ``path`` (relative to the base URL); return the
        decoded JSON response object."""

    def get(self, path: str) -> Dict[str, Any]:
        """GET ``path`` (relative to the base URL); return the decoded JSON."""


class _NotFound(Exception):
    """Raised by a transport's ``get`` for an HTTP 404, so the client can apply
    the legacy-discovery-path fallback. Transports that cannot distinguish 404
    simply never raise this."""


class _UrllibTransport:
    """Default stdlib ``urllib`` transport (no third-party deps).

    POSTs JSON-RPC frames to ``{base_url}{path}`` and GETs JSON documents,
    decoding the response body as JSON. Adds ``Authorization: Bearer {token}``
    when a token is configured. A 404 on ``get`` is surfaced as :class:`_NotFound`
    so :meth:`A2AClient.fetch_agent_card` can fall back to the legacy path.
    """

    def __init__(self, base_url: str, *, token: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self, *, content_type: bool) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        return headers

    def post(self, path: str, json_body: Mapping[str, Any]) -> Dict[str, Any]:
        data = json_dumps(json_body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=self._headers(content_type=True),
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310 - explicit URL
            return _decode_json(resp.read())

    def get(self, path: str) -> Dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path,
            headers=self._headers(content_type=False),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req) as resp:  # noqa: S310 - explicit URL
                return _decode_json(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise _NotFound(path) from exc
            raise


def _decode_json(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise A2AClientError(-32603, "expected a JSON object response, got %r" % type(value).__name__)
    return value


class A2AClient:
    """Outbound A2A client for delegating work to a remote A2A agent.

    ``base_url`` is the remote agent's origin (scheme + host[:port]); the
    JSON-RPC endpoint is ``{base_url}/a2a`` (per its AgentCard). ``token``, when
    set, is sent as a bearer credential by the default transport. ``transport``
    overrides the default :class:`_UrllibTransport` -- inject a fake to route
    in-process for tests.

    The client is stateless beyond its transport; methods reuse
    :mod:`mac.a2a.protocol` types for (de)serialization.
    """

    #: The remote A2A JSON-RPC endpoint, relative to ``base_url``. Mirrors the
    #: inbound ``A2A_ENDPOINT_PATH``; a peer's card may advertise the same.
    RPC_PATH = "/a2a"

    def __init__(
        self,
        base_url: str,
        *,
        token: Optional[str] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.transport: Transport = transport or _UrllibTransport(
            self.base_url, token=token
        )
        # Monotonic per-client JSON-RPC request id (string form for stability).
        self._next_id = 0

    # -- discovery ----------------------------------------------------------

    def fetch_agent_card(self) -> Dict[str, Any]:
        """GET the remote agent's AgentCard discovery document.

        Tries the canonical ``/.well-known/agent-card.json`` (A2A v0.3+); on a
        404 it falls back to the pre-v0.3 ``/.well-known/agent.json`` alias so
        older agents still resolve. Returns the card as a plain dict.
        """

        try:
            return self.transport.get(WELL_KNOWN_CARD_PATH)
        except _NotFound:
            return self.transport.get(WELL_KNOWN_CARD_PATH_LEGACY)

    # -- delegation ---------------------------------------------------------

    def send_message(
        self,
        text: str,
        *,
        context_id: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> Task:
        """``message/send``: delegate ``text`` as a new unit of work.

        Builds a ``user``-role :class:`~mac.a2a.protocol.Message` with a single
        text ``Part`` (generating a ``messageId`` when not supplied), POSTs the
        ``message/send`` request, and parses the result into a
        :class:`~mac.a2a.protocol.Task`.
        """

        message = Message(
            role=Role.USER,
            parts=[text_part(text)],
            message_id=message_id or new_message_id(),
            context_id=context_id,
        )
        params: Dict[str, Any] = {"message": message.to_dict()}
        if context_id is not None:
            params["contextId"] = context_id
        return self._rpc_task(Method.MESSAGE_SEND, params)

    def get_task(self, task_id: str) -> Task:
        """``tasks/get``: fetch the current state of a delegated task."""

        return self._rpc_task(Method.TASKS_GET, {"id": task_id})

    def cancel_task(self, task_id: str) -> Task:
        """``tasks/cancel``: request cancellation of a delegated task."""

        return self._rpc_task(Method.TASKS_CANCEL, {"id": task_id})

    # -- internals ----------------------------------------------------------

    def _rpc_task(self, method: str, params: Mapping[str, Any]) -> Task:
        """Make a JSON-RPC call whose ``result`` is an A2A Task; parse it."""

        result = self._call(method, params)
        if not isinstance(result, Mapping):
            raise A2AClientError(
                -32603, "expected a Task object in result, got %r" % type(result).__name__
            )
        return Task.from_dict(result)

    def _call(self, method: str, params: Mapping[str, Any]) -> Any:
        """POST one JSON-RPC request and return its ``result`` (or raise).

        Raises :class:`A2AClientError` carrying the JSON-RPC ``code`` /
        ``message`` when the response is an error envelope.
        """

        request = {
            "jsonrpc": JSONRPC_VERSION,
            "id": self._make_id(),
            "method": method,
            "params": dict(params),
        }
        response = self.transport.post(self.RPC_PATH, request)
        if not isinstance(response, Mapping):
            raise A2AClientError(
                -32603, "expected a JSON-RPC response object, got %r" % type(response).__name__
            )
        error = response.get("error")
        if error is not None:
            if isinstance(error, Mapping):
                raise A2AClientError(
                    int(error.get("code", -32603)),
                    str(error.get("message", "unknown error")),
                    error.get("data"),
                )
            raise A2AClientError(-32603, "malformed JSON-RPC error: %r" % (error,))
        return response.get("result")

    def _make_id(self) -> str:
        self._next_id += 1
        return "a2a-out-%d" % self._next_id
