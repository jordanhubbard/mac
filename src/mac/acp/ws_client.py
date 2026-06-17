"""Outbound WebSocket transport for the ACP **host/client** role (ADR 0006).

This is the deferred counterpart to :mod:`mac.acp.ws` (the *server* side, where a
remote ACP client drives a mac agent over WS). Here mac dials **out** to a remote
ACP agent over WebSocket and drives it with :class:`~mac.acp.client.ACPClient`.

Where the server side bridges an *async* Starlette ``WebSocket`` to the
synchronous :class:`~mac.acp.peer.Peer`, the client side has no async at all: it
uses the **synchronous** ``websocket-client`` library (the ``websocket`` module),
which fits the peer's existing threaded model exactly the way
:func:`~mac.acp.peer.stdio_peer` wires a subprocess:

* **Outbound (peer -> socket).** The peer is constructed with a ``send(bytes)``
  callable that decodes the single newline-terminated frame back to one text
  payload and calls ``connection.send(text)``. ``websocket-client`` sockets are
  safe to send on from any thread for our fire-and-forget framing, so the client
  can issue requests from its own thread while the reader thread feeds responses.
* **Inbound (socket -> peer).** A background reader thread loops
  ``connection.recv()`` and feeds each text frame into the peer
  (``feed(text.encode() + b"\\n")`` then ``pump()``), exactly mirroring
  :meth:`mac.acp.peer._StdioPeer._read_loop`. Each WS frame carries exactly one
  JSON-RPC message, so the appended newline lets the peer's existing framing
  parse it. ``pump()`` returns immediately, so a streamed run (many
  ``session/update`` notifications followed by the final response) is delivered
  frame-by-frame while the caller's blocking ``request().result()`` waits.

Authentication mirrors the server in :mod:`mac.api` / :mod:`mac.acp.ws`, which
accepts a bearer token either as an ``Authorization: Bearer <token>`` header or
as the ``Authorization`` WebSocket subprotocol (``["Authorization", "<token>"]``,
the browser-friendly path). :func:`connect_acp_websocket` sends both by default
when a ``token`` is given, so it works against either acceptance path.

The ``websocket`` import is **guarded**: importing this module never hard-fails if
``websocket-client`` is absent. The dependency is required only when actually
dialing a real socket; injecting a ``connection=`` (the test seam) needs nothing.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .client import ACPClient
from .peer import Peer
from .protocol import ClientCapabilities


__all__ = ["connect_acp_websocket", "ACPWebSocketClient", "WSConnection"]


def _load_websocket() -> Any:
    """Resolve the ``websocket`` module lazily so importing this module is cheap.

    Keeping the import out of module scope means ``import mac.acp.ws_client``
    never fails when ``websocket-client`` is unavailable; the cost (and a clear
    error if it is missing) is paid only when :func:`connect_acp_websocket` is
    actually asked to dial a real socket. Returns the ``websocket`` module.
    """

    try:
        import websocket  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise RuntimeError(
            "connect_acp_websocket requires the 'websocket-client' package "
            "(the 'websocket' module) to dial a real socket; install it or pass "
            "connection= to inject a transport."
        ) from exc
    return websocket


class WSConnection:
    """Minimal duck-typed protocol for the injectable transport.

    Anything with ``send(str)``, ``recv() -> str``, and ``close()`` works -- a
    real ``websocket-client`` ``WebSocket`` satisfies it, and tests inject a fake
    backed by in-memory queues. This is documentation-only (the code never
    isinstance-checks it); it exists so the seam's contract is explicit.
    """

    def send(self, data: str) -> None:  # pragma: no cover - protocol shape
        ...

    def recv(self) -> str:  # pragma: no cover - protocol shape
        ...

    def close(self) -> None:  # pragma: no cover - protocol shape
        ...


class _WebSocketPeer(Peer):
    """A :class:`Peer` bound to a synchronous WebSocket connection.

    A background reader thread streams inbound text frames into :meth:`feed` and
    drives :meth:`pump`, so callers interact purely through the :class:`Peer` API
    (``request`` / ``notify`` + registered handlers) -- the WS analogue of
    :class:`~mac.acp.peer._StdioPeer`.
    """

    def __init__(self, connection: Any) -> None:
        self._conn = connection
        super().__init__(send=self._send_to_socket)
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _send_to_socket(self, data: bytes) -> None:
        # The peer hands us a single newline-terminated frame; one JSON-RPC
        # message per WS text frame, so strip the trailing newline.
        if self._stop.is_set():
            return
        text = data.decode("utf-8").rstrip("\n")
        self._conn.send(text)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._conn.recv()
            except Exception:  # noqa: BLE001 - socket closed / dropped -> stop reading
                break
            if message is None or message == "":
                # Empty frame on a closed/closing socket -> treat as EOF.
                break
            if isinstance(message, bytes):
                data = message
            else:
                data = message.encode("utf-8")
            # Each frame is one JSON-RPC message; append "\n" so the peer's
            # existing newline framing parses it, then pump to dispatch.
            self.feed(data + b"\n")
            self.pump()

    def close(self) -> None:
        """Stop the reader thread and close the underlying connection."""

        self._stop.set()
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass


def connect_acp_websocket(
    url: Optional[str] = None,
    *,
    token: Optional[str] = None,
    header: Optional[Any] = None,
    subprotocols: Optional[List[str]] = None,
    connection: Optional[Any] = None,
    **ws_opts: Any,
) -> Peer:
    """Open an outbound ACP WebSocket and return a :class:`Peer` wired to it.

    The returned peer exposes a ``close()`` method (like
    :func:`~mac.acp.peer.stdio_peer`) that stops the reader thread and closes the
    socket; the caller owns its teardown.

    Parameters
    ----------
    url:
        The ``ws://`` / ``wss://`` URL of the remote ACP agent. Required unless
        ``connection`` is injected.
    token:
        Optional bearer token. When set, it is sent **both** as an
        ``Authorization: Bearer <token>`` header and as the ``Authorization``
        WebSocket subprotocol (``["Authorization", "<token>"]``), matching the two
        acceptance paths of mac's server (:func:`mac.api._authorize_acp_websocket`
        / :mod:`mac.acp.ws`). This works against either, including browser-style
        servers that can only read the subprotocol.
    header:
        Optional extra headers (list of ``"Name: value"`` strings or a dict),
        merged with the ``Authorization`` header derived from ``token``.
    subprotocols:
        Optional explicit subprotocol list. When ``token`` is set and this is
        omitted, ``["Authorization", "<token>"]`` is offered automatically.
    connection:
        Injectable transport seam. When provided, it is used verbatim instead of
        dialing a real socket; it must implement ``send(str)``, ``recv() -> str``,
        and ``close()`` (see :class:`WSConnection`). Tests inject a fake; ``url``,
        ``token``, ``header`` and ``subprotocols`` are then ignored, and the
        ``websocket-client`` package is not required.
    **ws_opts:
        Extra keyword options forwarded to ``websocket.create_connection``
        (e.g. ``timeout=``, ``sslopt=``).

    Returns
    -------
    Peer
        A live peer; its ``close`` attribute tears down the reader + socket.
    """

    if connection is None:
        if not url:
            raise ValueError("connect_acp_websocket requires a url (or connection=)")
        websocket = _load_websocket()

        headers = _normalize_headers(header)
        offered_subprotocols = list(subprotocols) if subprotocols is not None else None
        if token:
            # Header path: Authorization: Bearer <token>.
            headers.append("Authorization: Bearer %s" % token)
            # Subprotocol path: ["Authorization", "<token>"] (browser-friendly).
            if offered_subprotocols is None:
                offered_subprotocols = ["Authorization", token]

        create_kwargs: Dict[str, Any] = dict(ws_opts)
        if headers:
            create_kwargs["header"] = headers
        if offered_subprotocols is not None:
            create_kwargs["subprotocols"] = offered_subprotocols

        connection = websocket.create_connection(url, **create_kwargs)

    return _WebSocketPeer(connection)


def _normalize_headers(header: Optional[Any]) -> List[str]:
    """Coerce ``header`` (None / dict / list of "Name: value") to a list."""

    if header is None:
        return []
    if isinstance(header, dict):
        return ["%s: %s" % (k, v) for k, v in header.items()]
    return list(header)


class ACPWebSocketClient:
    """Convenience wrapper: an :class:`ACPClient` driving a remote agent over WS.

    Wires ``ACPClient(connect_acp_websocket(...))`` so a caller can run
    ``initialize`` / ``session_new`` / ``session_prompt`` against a remote ACP
    agent over WebSocket, then :meth:`close` to tear the transport down. Use it as
    a context manager to guarantee cleanup::

        with ACPWebSocketClient("wss://host/acp/ws", token=tok) as client:
            client.initialize()
            session_id = client.session_new(cwd="/work")
            result = client.session_prompt(session_id, "do the thing")

    The wrapped :class:`ACPClient` is exposed as :attr:`client` (and proxied via
    ``__getattr__``) for any method not surfaced directly here.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        *,
        token: Optional[str] = None,
        header: Optional[Any] = None,
        subprotocols: Optional[List[str]] = None,
        connection: Optional[Any] = None,
        client_capabilities: Optional[ClientCapabilities] = None,
        client_info: Optional[Dict[str, Any]] = None,
        **ws_opts: Any,
    ) -> None:
        self._peer = connect_acp_websocket(
            url,
            token=token,
            header=header,
            subprotocols=subprotocols,
            connection=connection,
            **ws_opts,
        )
        self.client = ACPClient(
            self._peer,
            client_capabilities=client_capabilities,
            client_info=client_info,
        )

    @property
    def peer(self) -> Peer:
        """The underlying :class:`Peer` (its ``close`` tears down the transport)."""

        return self._peer

    def close(self) -> None:
        """Close the WebSocket transport and stop its reader thread."""

        closer = getattr(self._peer, "close", None)
        if callable(closer):
            closer()

    def __getattr__(self, name: str) -> Any:
        # Proxy ACPClient methods (initialize / session_new / session_prompt /
        # on_update / ...) so callers can drive the remote agent directly.
        return getattr(self.client, name)

    def __enter__(self) -> "ACPWebSocketClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
