"""``ACPAgentServer`` -- the agent/server side of the Agent Client Protocol.

This is the role mac plays when it is *driven by* an external ACP-compliant
client (Zed, another hub, or mac's own :class:`~mac.acp.client.ACPClient`). It
wraps a symmetric :class:`~mac.acp.peer.Peer` and registers handlers for the
client-initiated agent methods (``initialize``, ``authenticate``,
``session/new``, ``session/load``, ``session/prompt``) plus the inbound
``session/cancel`` notification. During a prompt turn it streams progress back to
the client as ``session/update`` notifications and may ask the client for a
decision via a ``session/request_permission`` request.

The actual "do the work" logic is intentionally **not** baked in here. A
prompt turn is handed to a :class:`PromptBackend` -- a clean seam where mac's
task/tool execution will plug in (the production backend is the deliberate
Phase-2 follow-up). For the ``python -m mac.acp.server`` entrypoint this module
ships a minimal :class:`EchoBackend` so the server is runnable standalone; it
does **not** import or touch ``task_executor``, ``api``, ``services``, or any
other existing module.

Transport: stdio only for this phase (newline-delimited JSON-RPC). A remote
WebSocket transport is a later Phase-2b note (see ADR 0006).

Spec notes (verified against ``agentclientprotocol.com/protocol/v1/*``):

* ``initialize`` negotiates a single integer ``protocolVersion`` (MAJOR
  version). The agent answers with the lower of its own and the client's
  version, never claiming a higher version than the client requested.
* ``session/update`` is an agent -> client **notification** with the shape
  ``{"sessionId": ..., "update": {"sessionUpdate": <kind>, ...}}``.
* ``session/request_permission`` is an agent -> client **request**; the result
  nests the decision under ``outcome``.
* ``session/cancel`` is a client -> agent **notification** (no response).
"""

from __future__ import annotations

import itertools
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Protocol, Union, runtime_checkable

from .peer import DEFERRED, Peer, RemoteError
from .protocol import (
    ERROR_INTERNAL,
    ERROR_INVALID_PARAMS,
    ERROR_METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    AgentCapabilities,
    AuthenticateParams,
    AuthMethod,
    InitializeParams,
    InitializeResult,
    JSONRPCRequest,
    JSONRPCError,
    Method,
    NewSessionParams,
    NewSessionResult,
    PermissionOption,
    PromptParams,
    PromptResult,
    RequestPermissionParams,
    RequestPermissionResult,
    SessionUpdateKind,
    StopReason,
    text_block,
)


__all__ = [
    "PromptTurn",
    "PromptBackend",
    "PromptBackendFn",
    "ACPAgentServer",
    "EchoBackend",
    "serve_stdio",
    "main",
]


# ---------------------------------------------------------------------------
# Session + turn bookkeeping
# ---------------------------------------------------------------------------


class _Session:
    """Server-side state for a single ACP session.

    Holds the allocated ``session_id`` and its ``cwd`` (advertised by the client
    at ``session/new``), plus the live turn's :class:`PromptTurn` (if any) so an
    inbound ``session/cancel`` can flag it.
    """

    def __init__(self, session_id: str, cwd: str) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.current_turn: Optional["PromptTurn"] = None


class PromptTurn:
    """One ``session/prompt`` turn handed to the backend.

    Carries the turn's identity (:attr:`session_id`, :attr:`cwd`) and the prompt
    :attr:`content` blocks, and exposes the agent-side channels back to the
    client:

    * :meth:`send_update` (+ the :meth:`agent_message_chunk` / :meth:`tool_call`
      helpers) sends a ``session/update`` notification.
    * :meth:`request_permission` sends a ``session/request_permission`` request
      and blocks for the client's decision.
    * :attr:`cancelled` is a flag the backend can poll; it is set when an inbound
      ``session/cancel`` notification arrives for this session.
    """

    def __init__(
        self,
        peer: Peer,
        session_id: str,
        content: List[Dict[str, Any]],
        *,
        cwd: str = "",
        permission_timeout: Optional[float] = None,
    ) -> None:
        self._peer = peer
        self.session_id = session_id
        self.cwd = cwd
        self.content = content
        self._permission_timeout = permission_timeout
        self._cancelled = threading.Event()

    # -- cancellation --------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        """``True`` once a ``session/cancel`` arrived for this turn's session."""

        return self._cancelled.is_set()

    def _mark_cancelled(self) -> None:
        self._cancelled.set()

    # -- streaming updates (agent -> client notifications) -------------------

    def send_update(self, update: Dict[str, Any]) -> None:
        """Send a ``session/update`` notification wrapping ``update``.

        ``update`` must be the inner object carrying its own ``sessionUpdate``
        discriminator (e.g. ``{"sessionUpdate": "agent_message_chunk", ...}``);
        this method wraps it into the
        ``{"sessionId": ..., "update": {...}}`` envelope the spec requires.
        """

        self._peer.notify(
            Method.SESSION_UPDATE,
            {"sessionId": self.session_id, "update": dict(update)},
        )

    def agent_message_chunk(self, text: str) -> None:
        """Stream a chunk of the agent's reply as an ``agent_message_chunk``."""

        self.send_update(
            {
                "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                "content": text_block(text),
            }
        )

    def agent_thought_chunk(self, text: str) -> None:
        """Stream a chunk of the agent's reasoning as an ``agent_thought_chunk``."""

        self.send_update(
            {
                "sessionUpdate": SessionUpdateKind.AGENT_THOUGHT_CHUNK,
                "content": text_block(text),
            }
        )

    def tool_call(
        self,
        tool_call_id: str,
        title: str,
        *,
        kind: Optional[str] = None,
        status: Optional[str] = None,
        **extra: Any,
    ) -> None:
        """Stream a ``tool_call`` update announcing a tool invocation.

        ``tool_call_id`` and ``title`` are the spec-required fields; ``kind`` and
        ``status`` are common optionals, and any further keyword args are merged
        verbatim into the update object.
        """

        update: Dict[str, Any] = {
            "sessionUpdate": SessionUpdateKind.TOOL_CALL,
            "toolCallId": tool_call_id,
            "title": title,
        }
        if kind is not None:
            update["kind"] = kind
        if status is not None:
            update["status"] = status
        update.update(extra)
        self.send_update(update)

    def tool_call_update(
        self,
        tool_call_id: str,
        *,
        status: Optional[str] = None,
        **extra: Any,
    ) -> None:
        """Stream a ``tool_call_update`` for an in-flight tool call."""

        update: Dict[str, Any] = {
            "sessionUpdate": SessionUpdateKind.TOOL_CALL_UPDATE,
            "toolCallId": tool_call_id,
        }
        if status is not None:
            update["status"] = status
        update.update(extra)
        self.send_update(update)

    # -- permission (agent -> client request) --------------------------------

    def request_permission(
        self,
        tool_call: Dict[str, Any],
        options: List[PermissionOption],
        *,
        timeout: Optional[float] = None,
    ) -> RequestPermissionResult:
        """Ask the client to authorize ``tool_call`` and block for the decision.

        Sends a ``session/request_permission`` request and returns the parsed
        :class:`RequestPermissionResult`. If the client returns a JSON-RPC error
        (e.g. it does not support permissions), the decision is treated as a
        ``cancelled`` outcome rather than propagating the error into the backend.
        """

        params = RequestPermissionParams(
            session_id=self.session_id,
            tool_call=tool_call,
            options=list(options),
        )
        pending = self._peer.request(
            Method.SESSION_REQUEST_PERMISSION, params.to_dict()
        )
        effective_timeout = timeout if timeout is not None else self._permission_timeout
        try:
            raw = pending.result(effective_timeout)
        except RemoteError:
            return RequestPermissionResult(outcome="cancelled")
        return RequestPermissionResult.from_dict(raw or {})


# ---------------------------------------------------------------------------
# Backend seam
# ---------------------------------------------------------------------------


@runtime_checkable
class PromptBackend(Protocol):
    """The seam that turns one :class:`PromptTurn` into a stop reason.

    Implementations do the actual work of a prompt turn -- streaming updates and
    requesting permission via the turn's methods -- and return a
    :class:`~mac.acp.protocol.StopReason` value (e.g.
    :data:`StopReason.END_TURN`). This is where mac's task/tool execution will
    later plug in; it is deliberately decoupled from the protocol plumbing.

    A plain callable ``(PromptTurn) -> str`` also satisfies this protocol, so
    backends can be written as a class with :meth:`run_prompt` or as a bare
    function (see :data:`PromptBackendFn`).
    """

    def run_prompt(self, turn: PromptTurn) -> str:  # pragma: no cover - protocol
        ...


#: A bare-callable backend: ``(turn) -> stop_reason``. Accepted anywhere a
#: :class:`PromptBackend` is, via :func:`_as_backend`.
PromptBackendFn = Callable[[PromptTurn], str]


def _as_backend(backend: Union[PromptBackend, PromptBackendFn]) -> PromptBackend:
    """Normalize a backend (object with ``run_prompt`` or bare callable)."""

    if hasattr(backend, "run_prompt"):
        return backend  # type: ignore[return-value]
    if callable(backend):
        return _CallableBackend(backend)
    raise TypeError(
        "backend must be a PromptBackend (have run_prompt) or a callable "
        "(PromptTurn) -> str"
    )


class _CallableBackend:
    """Adapt a bare ``(turn) -> stop_reason`` callable to the backend protocol."""

    def __init__(self, fn: PromptBackendFn) -> None:
        self._fn = fn

    def run_prompt(self, turn: PromptTurn) -> str:
        return self._fn(turn)


class EchoBackend:
    """A minimal default backend for the standalone entrypoint.

    It streams the incoming text back as a single ``agent_message_chunk`` and
    ends the turn. It does **no** task/tool execution -- the production backend
    that binds to mac's task/tool surface is the Phase-2 follow-up. Use this only
    to smoke-test ``python -m mac.acp.server`` against a real client.
    """

    def run_prompt(self, turn: PromptTurn) -> str:
        if turn.cancelled:
            return StopReason.CANCELLED
        text = _prompt_text(turn.content)
        turn.agent_message_chunk("echo: %s" % text if text else "echo")
        return StopReason.END_TURN


def _prompt_text(content: List[Dict[str, Any]]) -> str:
    """Concatenate the text of all ``text`` content blocks in a prompt."""

    parts = [
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


class ACPAgentServer:
    """Agent-side ACP endpoint: answers a client's requests over a :class:`Peer`.

    Parameters
    ----------
    peer:
        A ready :class:`Peer`. The server registers its inbound handlers on it.
    backend:
        A :class:`PromptBackend` (or bare ``(turn) -> stop_reason`` callable)
        that runs each prompt turn.
    agent_capabilities:
        Advertised in the ``initialize`` result. Defaults to a no-frills
        :class:`AgentCapabilities` with ``loadSession=False`` (so ``session/load``
        is rejected as unsupported).
    agent_info:
        Optional ``{"name": ..., "version": ...}`` advertised in ``initialize``.
    auth_methods:
        Optional list of :class:`AuthMethod` advertised in ``initialize``. When
        empty, ``authenticate`` still succeeds (no auth required for this phase).
    permission_timeout:
        Optional timeout (seconds) for blocking on a client's permission
        decision inside :meth:`PromptTurn.request_permission`.
    """

    def __init__(
        self,
        peer: Peer,
        backend: Union[PromptBackend, PromptBackendFn],
        *,
        agent_capabilities: Optional[AgentCapabilities] = None,
        agent_info: Optional[Dict[str, Any]] = None,
        auth_methods: Optional[List[AuthMethod]] = None,
        permission_timeout: Optional[float] = None,
    ) -> None:
        self._peer = peer
        self._backend = _as_backend(backend)
        self._agent_capabilities = agent_capabilities or AgentCapabilities()
        self._agent_info = agent_info or {
            "name": "mac",
            "title": "MAC agent",
            "version": "0",
        }
        self._auth_methods = list(auth_methods or [])
        self._permission_timeout = permission_timeout

        self._sessions: Dict[str, _Session] = {}
        self._session_ids = itertools.count(1)
        self._lock = threading.Lock()
        self._active_turns = 0
        self._idle = threading.Event()
        self._idle.set()  # no turns in flight yet

        # Wire the client-initiated channels into the peer.
        peer.on_request(Method.INITIALIZE, self._handle_initialize)
        peer.on_request(Method.AUTHENTICATE, self._handle_authenticate)
        peer.on_request(Method.SESSION_NEW, self._handle_session_new)
        peer.on_request(Method.SESSION_LOAD, self._handle_session_load)
        # A prompt turn runs the (potentially blocking) backend on a worker
        # thread so the pump loop stays free to deliver the permission response
        # and any session/cancel that arrive *during* the turn. The response is
        # therefore deferred (see Peer.on_request_raw / DEFERRED).
        peer.on_request_raw(Method.SESSION_PROMPT, self._handle_session_prompt)
        peer.on_notification(Method.SESSION_CANCEL, self._handle_session_cancel)

    # -- agent methods (client -> agent requests) ----------------------------

    def _handle_initialize(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        parsed = InitializeParams.from_dict(params or {})
        # Negotiate: never claim a higher MAJOR version than the client offered.
        negotiated = min(parsed.protocol_version, PROTOCOL_VERSION)
        return InitializeResult(
            protocol_version=negotiated,
            agent_capabilities=self._agent_capabilities,
            auth_methods=self._auth_methods,
            agent_info=self._agent_info,
        ).to_dict()

    def _handle_authenticate(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        # Accept any advertised method (or none). Parse to surface a clear
        # invalid-params error if methodId is missing/malformed.
        AuthenticateParams.from_dict(params or {})
        return {}

    def _handle_session_new(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        parsed = NewSessionParams.from_dict(params or {})
        with self._lock:
            session_id = "sess_%d" % next(self._session_ids)
            self._sessions[session_id] = _Session(session_id, parsed.cwd)
        return NewSessionResult(session_id=session_id).to_dict()

    def _handle_session_load(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        # Loading prior sessions is an optional capability (loadSession). We
        # advertise it off by default; if the agent's capabilities say it is
        # unsupported, reject with a method-not-found-style error. If a future
        # backend turns loadSession on, this becomes the place to rehydrate.
        if not self._agent_capabilities.load_session:
            raise RemoteError(
                JSONRPCError(
                    code=ERROR_METHOD_NOT_FOUND,
                    message="session/load not supported (loadSession=false)",
                )
            )
        # loadSession advertised on but no rehydration backend yet -> surface a
        # clear error rather than silently fabricate a session.
        raise RemoteError(
            JSONRPCError(
                code=ERROR_METHOD_NOT_FOUND,
                message="session/load not implemented",
            )
        )

    def _handle_session_prompt(self, request: JSONRPCRequest) -> Any:
        parsed = PromptParams.from_dict(request.params or {})
        with self._lock:
            session = self._sessions.get(parsed.session_id)
            if session is None:
                # Reply synchronously -- no turn to run.
                raise RemoteError(
                    JSONRPCError(
                        code=ERROR_INVALID_PARAMS,
                        message="unknown session: %s" % parsed.session_id,
                    )
                )
            turn = PromptTurn(
                self._peer,
                parsed.session_id,
                parsed.prompt,
                cwd=session.cwd,
                permission_timeout=self._permission_timeout,
            )
            session.current_turn = turn
            self._active_turns += 1
            self._idle.clear()

        # Run the backend off the pump thread; reply when it finishes. This keeps
        # the inbound loop free to deliver the client's permission decision and
        # any session/cancel notification while the turn is in flight.
        request_id = request.id
        threading.Thread(
            target=self._run_turn, args=(request_id, session, turn), daemon=True
        ).start()
        return DEFERRED

    def _run_turn(
        self, request_id: Any, session: "_Session", turn: "PromptTurn"
    ) -> None:
        try:
            stop_reason = self._backend.run_prompt(turn)
            # A backend that returns nothing is a clean end of turn.
            result = PromptResult(
                stop_reason=stop_reason or StopReason.END_TURN
            ).to_dict()
            self._peer.respond_result(request_id, result)
        except RemoteError as exc:
            self._peer.respond_error(request_id, exc.error)
        except Exception as exc:  # noqa: BLE001 -- surface backend failure as a wire error
            self._peer.respond_error(
                request_id, JSONRPCError(code=ERROR_INTERNAL, message=str(exc))
            )
        finally:
            with self._lock:
                if session.current_turn is turn:
                    session.current_turn = None
                self._active_turns -= 1
                if self._active_turns <= 0:
                    self._idle.set()

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Block until no prompt turns are in flight (or ``timeout`` elapses).

        Returns ``True`` if the server became idle. Used by :func:`serve_stdio`
        to let an in-flight turn finish writing its response before the process
        exits on stdin EOF.
        """

        return self._idle.wait(timeout)

    # -- agent notifications (client -> agent) --------------------------------

    def _handle_session_cancel(self, params: Optional[Dict[str, Any]]) -> None:
        session_id = str((params or {}).get("sessionId", ""))
        with self._lock:
            session = self._sessions.get(session_id)
            turn = session.current_turn if session is not None else None
        if turn is not None:
            turn._mark_cancelled()

    # -- introspection (handy for tests / debugging) --------------------------

    def session_ids(self) -> List[str]:
        with self._lock:
            return list(self._sessions)


# ---------------------------------------------------------------------------
# stdio binding + entrypoint
# ---------------------------------------------------------------------------


def serve_stdio(
    backend: Union[PromptBackend, PromptBackendFn],
    *,
    stdin: Any = None,
    stdout: Any = None,
    **server_kwargs: Any,
) -> None:
    """Run an :class:`ACPAgentServer` over this process's stdio until EOF.

    Mirrors the stdio framing in :mod:`mac.acp.peer`: newline-delimited JSON-RPC,
    reading frames from ``stdin`` and writing to ``stdout``. Blocks the calling
    thread, pumping inbound frames, until stdin reaches EOF (the client closed
    the connection).

    ``stdin`` / ``stdout`` default to the process's binary stdio buffers; they
    are injectable for testing. Extra ``server_kwargs`` are forwarded to
    :class:`ACPAgentServer`.
    """

    in_stream = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)
    out_stream = (
        stdout if stdout is not None else getattr(sys.stdout, "buffer", sys.stdout)
    )

    out_lock = threading.Lock()

    def _send(data: bytes) -> None:
        with out_lock:
            out_stream.write(data)
            out_stream.flush()

    peer = Peer(send=_send)
    server = ACPAgentServer(peer, backend, **server_kwargs)

    try:
        while True:
            line = in_stream.readline()
            if not line:  # EOF -- client closed stdin
                break
            peer.feed_and_pump(
                line if isinstance(line, bytes) else line.encode("utf-8")
            )
    finally:
        # Let any in-flight turn finish writing its response before we return
        # (and the interpreter starts tearing down stdout under the worker).
        server.wait_idle(timeout=5)


def main(argv: Optional[List[str]] = None) -> int:
    """Entrypoint for ``python -m mac.acp.server``.

    Runs the server over stdio with the minimal :class:`EchoBackend`. This is a
    smoke-test entrypoint only -- the production backend that binds to mac's
    task/tool surface is the Phase-2 follow-up. The default backend echoes the
    prompt text back as a single agent message and ends the turn.
    """

    serve_stdio(EchoBackend())
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
