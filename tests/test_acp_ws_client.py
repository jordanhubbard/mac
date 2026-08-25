"""Outbound WebSocket ACP client transport tests (ADR 0006).

These exercise :func:`mac.acp.ws_client.connect_acp_websocket` -- mac dialing
*out* to a remote ACP agent over WebSocket -- without any real socket. A fake
``connection`` (the injectable seam) is backed by a queue and wired to a **real**
server-side :class:`~mac.acp.peer.Peer` driving an
:class:`~mac.acp.server.ACPAgentServer`, so the client's
``connection.send(text)`` feeds the server and the server's outbound frames flow
back through ``connection.recv()``. This mirrors the in-memory paired-transport
harness in ``test_acp_server.py`` / ``test_acp_ws.py`` -- no asyncio, no
subprocess, no third-party socket, no ``pytest-asyncio``.

The client's blocking calls (``initialize`` / ``session_new`` / ``session_prompt``)
run on a worker thread, like the other ACP tests, since the client peer's reader
thread must stay free to deliver responses while the call blocks.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, List, Optional

import pytest

from mac.acp.client import ACPClient
from mac.acp.peer import Peer
from mac.acp.protocol import (
    InitializeResult,
    PromptResult,
    SessionUpdateKind,
    StopReason,
)
from mac.acp.server import ACPAgentServer, PromptTurn
from mac.acp.ws_client import connect_acp_websocket


class StreamingBackend:
    """A scripted :class:`~mac.acp.server.PromptBackend`.

    During a turn it streams an ``agent_message_chunk`` and a ``tool_call``
    ``session/update`` back to the client (from the server's per-turn worker
    thread), then ends the turn -- the shape of a real prompt turn, subprocess
    free. Captures the turns it ran for assertions.
    """

    def __init__(self) -> None:
        self.turns: List[PromptTurn] = []

    def run_prompt(self, turn: PromptTurn) -> str:
        self.turns.append(turn)
        turn.agent_message_chunk("working on it")
        turn.tool_call("call_1", "run shell", kind="execute", status="pending")
        return StopReason.END_TURN


class FakeWSConnection:
    """A fake WebSocket ``connection`` wired to a real server-side peer.

    Implements the ``send(str)`` / ``recv() -> str`` / ``close()`` seam
    :func:`connect_acp_websocket` expects:

    * ``send(text)`` (the client peer's outbound) feeds the **server** peer one
      JSON-RPC frame and pumps it. The server's prompt handler defers the turn to
      its own worker thread, so ``pump`` returns immediately and never deadlocks
      against this caller.
    * The server peer's outbound ``send`` (responses + ``session/update``
      notifications, some emitted from the worker thread) enqueues text frames
      here; ``recv()`` blocks on that queue and hands them to the client peer's
      reader thread.
    * ``close()`` unblocks any pending ``recv`` with a sentinel so the client's
      reader thread exits cleanly.
    """

    _CLOSED = object()

    def __init__(self, backend: Any, **server_kwargs: Any) -> None:
        self._inbound: "queue.Queue[Any]" = queue.Queue()
        self.server_peer = Peer(send=self._server_outbound)
        self.server = ACPAgentServer(self.server_peer, backend, **server_kwargs)
        self._closed = False

    # -- server peer outbound -> client recv queue ---------------------------

    def _server_outbound(self, data: bytes) -> None:
        # One JSON-RPC message per WS frame: the peer hands us a single
        # newline-terminated frame; strip it to the text payload a WS carries.
        self._inbound.put(data.decode("utf-8").rstrip("\n"))

    # -- the WSConnection seam -----------------------------------------------

    def send(self, text: str) -> None:
        # Client -> server: feed one frame and pump. The server defers prompt
        # turns to a worker thread, so this returns promptly.
        self.server_peer.feed(text.encode("utf-8") + b"\n")
        self.server_peer.pump()

    def recv(self) -> str:
        item = self._inbound.get()
        if item is self._CLOSED:
            # EOF: returning "" makes the client's reader loop treat it as a
            # closed socket and stop.
            return ""
        return item

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._inbound.put(self._CLOSED)


def _run_in_thread(fn, *args, **kwargs):
    """Run ``fn`` on a worker thread; return a ``(thread, box)`` pair.

    The client's blocking calls must not run on the thread that also pumps the
    client peer -- here the client peer pumps on its own reader thread, so the
    blocking call runs on this worker while the reader delivers the response.
    """

    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - surface to the test
            box["error"] = exc

    thread = threading.Thread(target=_target)
    thread.start()
    return thread, box


def _await(thread, box, *, timeout: float = 5.0) -> Any:
    thread.join(timeout)
    assert not thread.is_alive(), "client call did not complete in time"
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_full_session_over_ws_client():
    """initialize -> session/new -> session/prompt against a remote agent over a
    fake WS connection: negotiated version, streamed updates, and stop reason."""

    backend = StreamingBackend()
    fake = FakeWSConnection(backend, agent_info={"name": "remote-agent", "version": "7"})

    peer = connect_acp_websocket(connection=fake)
    client = ACPClient(peer)

    received_updates: List[Dict[str, Any]] = []
    client.on_update(lambda update: received_updates.append(update))

    try:
        # --- initialize: the remote agent negotiates the protocol version ---
        t, box = _run_in_thread(client.initialize, timeout=5)
        init = _await(t, box)
        assert isinstance(init, InitializeResult)
        assert init.protocol_version == 1
        assert init.agent_info == {"name": "remote-agent", "version": "7"}

        # --- session/new allocates and returns an id ---
        t, box = _run_in_thread(client.session_new, "/tmp/work", timeout=5)
        session_id = _await(t, box)
        assert session_id
        assert session_id in fake.server.session_ids()

        # --- session/prompt drives the backend, which streams two updates ---
        t, box = _run_in_thread(client.session_prompt, session_id, "do the thing", timeout=5)
        result = _await(t, box)
        assert isinstance(result, PromptResult)
        assert result.stop_reason == StopReason.END_TURN
    finally:
        peer.close()

    # The backend ran exactly one turn carrying the session id, cwd, and prompt.
    assert len(backend.turns) == 1
    turn = backend.turns[0]
    assert turn.session_id == session_id
    assert turn.cwd == "/tmp/work"
    assert turn.content == [{"type": "text", "text": "do the thing"}]

    # The client's update handler saw both streamed notifications, in order,
    # delivered over the WS bridge (server worker thread -> recv queue -> client
    # reader thread).
    kinds = [u["update"]["sessionUpdate"] for u in received_updates]
    assert kinds == [
        SessionUpdateKind.AGENT_MESSAGE_CHUNK,
        SessionUpdateKind.TOOL_CALL,
    ]
    assert received_updates[0]["sessionId"] == session_id
    assert received_updates[0]["update"]["content"] == {
        "type": "text",
        "text": "working on it",
    }
    assert received_updates[1]["update"]["toolCallId"] == "call_1"


def test_peer_close_is_exposed():
    """The returned peer exposes a ``close`` (like ``stdio_peer``) that tears the
    transport down."""

    fake = FakeWSConnection(StreamingBackend())
    peer = connect_acp_websocket(connection=fake)
    assert callable(getattr(peer, "close", None))
    peer.close()


def test_connect_requires_websocket_module_when_no_connection(monkeypatch):
    """Without an injected ``connection`` and with ``websocket`` unavailable,
    ``connect_acp_websocket`` raises a clear error (not an opaque ImportError)."""

    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args: Any, **kwargs: Any):
        if name == "websocket":
            raise ImportError("No module named 'websocket'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    with pytest.raises(RuntimeError) as exc:
        connect_acp_websocket("ws://example.invalid/acp/ws", token="tok")
    assert "websocket-client" in str(exc.value)


def test_connect_requires_url_or_connection():
    """No url and no injected connection is a programming error, surfaced early."""

    with pytest.raises(ValueError):
        connect_acp_websocket()
