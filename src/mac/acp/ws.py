"""Remote WebSocket transport for the ACP **agent/server** role (ADR 0006).

ADR 0006 Phase 2 calls for "stdio first, WS for remote". :mod:`mac.acp.server`
already covers the stdio case; this module adds the remote one: it bridges a
Starlette/FastAPI :class:`~starlette.websockets.WebSocket` to the existing,
*synchronous* :class:`~mac.acp.peer.Peer` driving an
:class:`~mac.acp.server.ACPAgentServer`. Nothing here is ACP-specific beyond the
plumbing -- it reuses the unchanged server, peer, and backend machinery.

The async <-> sync bridge
=========================
The :class:`~mac.acp.peer.Peer` is deliberately transport-agnostic and
synchronous: you give it a ``send(bytes)`` callable, push inbound bytes via
:meth:`~mac.acp.peer.Peer.feed`, and drive it with
:meth:`~mac.acp.peer.Peer.pump`. A Starlette ``WebSocket`` is async. The two are
joined as follows:

* **Outbound (sync -> async).** We capture the running event loop once at
  connect time. The peer's ``send`` schedules ``websocket.send_text`` back onto
  that loop with :func:`asyncio.run_coroutine_threadsafe`. This is what makes the
  server's **DEFERRED** path safe: a prompt turn runs the backend on a *worker
  thread* (see :meth:`mac.acp.server.ACPAgentServer._run_turn`) and emits
  ``session/update`` notifications and the final response from that thread --
  ``run_coroutine_threadsafe`` is the documented, thread-safe way to push work
  onto a loop from another thread, so those frames reach the socket without
  touching loop internals off-thread.
* **Inbound (async -> sync).** The accept/receive loop awaits one text frame at a
  time. Each WS frame carries exactly one JSON-RPC message; appending a newline
  (``feed(text.encode() + b"\\n")``) lets the peer's existing newline framing
  parse it, and :meth:`~mac.acp.peer.Peer.pump` dispatches it. The pump returns
  immediately even for ``session/prompt`` because the server defers the turn to
  a worker thread, so the receive loop stays free to deliver the client's
  permission decision and any ``session/cancel`` that arrive mid-turn.

On disconnect we drain any in-flight turn (``server.wait_idle``) so a worker
thread is not still trying to write to a closed socket as we unwind.

Imports of Starlette types are **guarded**: importing :mod:`mac.acp.ws` never
hard-fails when Starlette is absent (it is a dep in practice via FastAPI, but the
ACP package proper depends only on the stdlib). The names are resolved lazily
inside :func:`serve_acp_websocket`.

Scope
=====
This is the **server** side only -- mac being *driven* by a remote ACP client
over WS. The WS *client* transport (mac dialing OUT to a remote ACP agent over
WebSocket) is explicitly deferred: it needs a websocket-client dependency, which
this phase does not add. See ADR 0006.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

from .peer import Peer
from .server import ACPAgentServer, PromptBackend, PromptBackendFn

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.websockets import WebSocket


__all__ = ["serve_acp_websocket"]


def _load_websocket_types() -> Any:
    """Resolve ``WebSocketDisconnect`` lazily so importing this module is cheap.

    Keeping the Starlette import out of module scope means ``import mac.acp.ws``
    never fails if Starlette is unavailable; the cost is paid only when a socket
    is actually served. Returns the ``WebSocketDisconnect`` exception class.
    """

    from starlette.websockets import WebSocketDisconnect

    return WebSocketDisconnect


async def serve_acp_websocket(
    websocket: "WebSocket",
    backend: Union[PromptBackend, PromptBackendFn],
    *,
    agent_capabilities: Any = None,
    agent_info: Optional[Dict[str, Any]] = None,
    auth_methods: Optional[List[Any]] = None,
    permission_timeout: Optional[float] = None,
    drain_timeout: Optional[float] = 5.0,
    accept_kwargs: Optional[Dict[str, Any]] = None,
) -> None:
    """Run an :class:`~mac.acp.server.ACPAgentServer` over a WebSocket until close.

    Bridges the async ``websocket`` to a synchronous
    :class:`~mac.acp.peer.Peer`/:class:`~mac.acp.server.ACPAgentServer` (see the
    module docstring for the loop-capture + ``run_coroutine_threadsafe`` design).

    Parameters
    ----------
    websocket:
        An *un-accepted* Starlette/FastAPI ``WebSocket``. This function calls
        ``accept()`` (pass ``accept_kwargs`` to e.g. negotiate a subprotocol).
        Callers that must reject the connection should do so (``close``) before
        calling this; once accepted here, the caller should not also accept.
    backend:
        The :class:`~mac.acp.server.PromptBackend` (or bare ``(turn) -> reason``
        callable) that runs each prompt turn -- unchanged from the stdio path.
    agent_capabilities, agent_info, auth_methods, permission_timeout:
        Forwarded verbatim to :class:`~mac.acp.server.ACPAgentServer`.
    drain_timeout:
        Seconds to wait for an in-flight turn to finish on disconnect before
        returning (``None`` waits indefinitely). Mirrors ``serve_stdio``.
    accept_kwargs:
        Optional kwargs for ``websocket.accept`` (e.g. ``{"subprotocol": ...}``).
    """

    websocket_disconnect = _load_websocket_types()

    # Capture the loop the socket lives on. The peer's send (called from the
    # inbound thread *and* from the server's per-turn worker threads) marshals
    # every outbound frame back onto this loop.
    loop = asyncio.get_running_loop()

    def _send(data: bytes) -> None:
        # One JSON-RPC message per WS frame: the peer hands us a single
        # newline-terminated frame, which we strip back to one text payload.
        text = data.decode("utf-8").rstrip("\n")
        # run_coroutine_threadsafe is the thread-safe bridge: it works from the
        # worker thread (DEFERRED responses / session/update) and from the loop's
        # own thread alike. We don't await the returned future -- delivery is
        # fire-and-forget from the peer's perspective, matching stdio's flush.
        asyncio.run_coroutine_threadsafe(websocket.send_text(text), loop)

    peer = Peer(send=_send)
    server = ACPAgentServer(
        peer,
        backend,
        agent_capabilities=agent_capabilities,
        agent_info=agent_info,
        auth_methods=auth_methods,
        permission_timeout=permission_timeout,
    )

    await websocket.accept(**(accept_kwargs or {}))
    try:
        while True:
            text = await websocket.receive_text()
            # Each frame is one JSON-RPC message; append "\n" so the peer's
            # existing newline framing parses it, then pump to dispatch. pump()
            # returns immediately even for session/prompt (the server defers the
            # turn to a worker thread), so the receive loop stays responsive to
            # the client's permission decision / session/cancel mid-turn.
            peer.feed(text.encode("utf-8") + b"\n")
            peer.pump()
    except websocket_disconnect:
        # Clean client-initiated close (or transport drop): fall through to drain.
        pass
    finally:
        # Let any in-flight turn finish writing its response before we unwind, so
        # a worker thread isn't left scheduling sends onto a dead socket. Run the
        # blocking wait off the event loop so we don't stall it.
        try:
            await loop.run_in_executor(None, server.wait_idle, drain_timeout)
        except Exception:  # noqa: BLE001 - draining is best-effort on teardown
            pass
