"""WebSocket (remote) ACP transport tests for the agent/server role (ADR 0006).

These drive the full ACP server over a real Starlette ``WebSocket``, using
Starlette/FastAPI's *synchronous* ``TestClient`` WebSocket support
(``client.websocket_connect(...)``), which runs the ASGI app in-process over an
in-memory transport -- no real socket, no ``wsproto``, no new third-party
dependency, and no ``pytest-asyncio``.

Each WS frame carries one JSON-RPC message (``ws.send_json`` / ``ws.receive_json``).
The server's prompt turn runs on a worker thread and emits its ``session/update``
notifications and final response from there via ``run_coroutine_threadsafe``, so
these tests also exercise the async-WS <-> sync-Peer bridge end to end.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import WebSocket
from fastapi.testclient import TestClient

from mac.acp.protocol import (
    PROTOCOL_VERSION,
    Method,
    SessionUpdateKind,
    StopReason,
    text_block,
)
from mac.acp.server import PromptTurn
from mac.api import create_app
from mac.services import ControlPlane


_TOKEN = "tok-acp-ws"


class StreamingBackend:
    """A fake :class:`~mac.acp.server.PromptBackend`.

    During a turn it streams an ``agent_message_chunk`` and a ``tool_call``
    ``session/update`` back to the client, then ends the turn -- mirroring the
    shape of a real prompt turn while staying subprocess-free. The chunks are
    emitted from the server's per-turn worker thread, so they exercise the
    ``run_coroutine_threadsafe`` outbound bridge.
    """

    def __init__(self) -> None:
        self.turns: List[PromptTurn] = []

    def run_prompt(self, turn: PromptTurn) -> str:
        self.turns.append(turn)
        turn.agent_message_chunk("working on it")
        turn.tool_call("call_1", "run shell", kind="execute", status="pending")
        return StopReason.END_TURN


def _app_with_backend(backend: Any, *, tokens: Dict[str, Any] | None = None):
    """Build a mac API app whose ``/acp/ws`` route serves ``backend``.

    The public route picks EchoBackend/MacAgentBackend itself; for the test we
    want a scripted backend, so we override the route to call
    ``serve_acp_websocket`` with our backend (reusing the app's auth helper).
    """

    from mac.acp.ws import serve_acp_websocket
    from mac.api import _authorize_acp_websocket

    cp = ControlPlane.in_memory()
    auth_tokens = {_TOKEN: ["agent"]} if tokens is None else tokens
    app = create_app(control_plane=cp, auth_tokens=auth_tokens)
    resolved_tokens = app.state.auth_tokens

    @app.websocket("/acp/ws-test")
    async def _acp_ws_test(websocket: WebSocket) -> None:
        principal, subproto = _authorize_acp_websocket(websocket, resolved_tokens)
        if principal is None and resolved_tokens:
            await websocket.close(code=1008)
            return
        accept_kwargs = {"subprotocol": subproto} if subproto else None
        await serve_acp_websocket(websocket, backend, accept_kwargs=accept_kwargs)

    return app


def test_full_session_over_websocket():
    backend = StreamingBackend()
    app = _app_with_backend(backend)
    client = TestClient(app)

    with client.websocket_connect("/acp/ws-test?token=%s" % _TOKEN) as ws:
        # --- initialize: the agent negotiates the protocol version ---
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": Method.INITIALIZE,
                "params": {"protocolVersion": PROTOCOL_VERSION},
            }
        )
        init = ws.receive_json()
        assert init["id"] == 1
        assert init["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert "agentCapabilities" in init["result"]

        # --- session/new: the agent allocates and returns a session id ---
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": Method.SESSION_NEW,
                "params": {"cwd": "/tmp/work", "mcpServers": []},
            }
        )
        new_session = ws.receive_json()
        assert new_session["id"] == 2
        session_id = new_session["result"]["sessionId"]
        assert session_id

        # --- session/prompt: drives the backend, which streams two updates ---
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": Method.SESSION_PROMPT,
                "params": {
                    "sessionId": session_id,
                    "prompt": [text_block("do the thing")],
                },
            }
        )

        # The two session/update notifications arrive as their own frames (in
        # order), emitted from the server's worker thread via the WS bridge.
        update_1 = ws.receive_json()
        assert update_1["method"] == Method.SESSION_UPDATE
        assert update_1["params"]["sessionId"] == session_id
        assert (
            update_1["params"]["update"]["sessionUpdate"]
            == SessionUpdateKind.AGENT_MESSAGE_CHUNK
        )
        assert update_1["params"]["update"]["content"] == {
            "type": "text",
            "text": "working on it",
        }

        update_2 = ws.receive_json()
        assert update_2["method"] == Method.SESSION_UPDATE
        assert (
            update_2["params"]["update"]["sessionUpdate"]
            == SessionUpdateKind.TOOL_CALL
        )
        assert update_2["params"]["update"]["toolCallId"] == "call_1"

        # ...followed by the deferred final response carrying the stop reason.
        prompt_result = ws.receive_json()
        assert prompt_result["id"] == 3
        assert prompt_result["result"]["stopReason"] == StopReason.END_TURN

    # The backend ran exactly one turn, carrying the session id, cwd, and prompt.
    assert len(backend.turns) == 1
    turn = backend.turns[0]
    assert turn.session_id == session_id
    assert turn.cwd == "/tmp/work"
    assert turn.content == [{"type": "text", "text": "do the thing"}]


def test_auth_subprotocol_accepted():
    """The bearer token may also ride as the second ``Authorization``
    subprotocol value (the browser-friendly path); the server echoes the chosen
    subprotocol back on accept."""

    backend = StreamingBackend()
    app = _app_with_backend(backend)
    client = TestClient(app)

    with client.websocket_connect(
        "/acp/ws-test", subprotocols=["Authorization", _TOKEN]
    ) as ws:
        assert ws.accepted_subprotocol == "Authorization"
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": Method.INITIALIZE,
                "params": {"protocolVersion": PROTOCOL_VERSION},
            }
        )
        init = ws.receive_json()
        assert init["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_missing_token_rejected():
    """No token, with tokens configured -> the socket is closed (policy 1008)."""

    import pytest
    from starlette.websockets import WebSocketDisconnect

    backend = StreamingBackend()
    app = _app_with_backend(backend)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/acp/ws-test") as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_bad_token_rejected():
    """A token that matches no registered principal -> the socket is closed."""

    import pytest
    from starlette.websockets import WebSocketDisconnect

    backend = StreamingBackend()
    app = _app_with_backend(backend)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/acp/ws-test?token=not-a-real-token") as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_default_route_uses_echo_backend():
    """The real ``/acp/ws`` route (no MAC_ACP_BACKEND_CMD) serves EchoBackend:
    a full session echoes the prompt text back and ends the turn."""

    cp = ControlPlane.in_memory()
    app = create_app(control_plane=cp, auth_tokens={_TOKEN: ["agent"]})
    client = TestClient(app)

    with client.websocket_connect("/acp/ws?token=%s" % _TOKEN) as ws:
        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": Method.INITIALIZE,
                "params": {"protocolVersion": PROTOCOL_VERSION},
            }
        )
        assert ws.receive_json()["result"]["protocolVersion"] == PROTOCOL_VERSION

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": Method.SESSION_NEW,
                "params": {"cwd": "/tmp", "mcpServers": []},
            }
        )
        session_id = ws.receive_json()["result"]["sessionId"]

        ws.send_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": Method.SESSION_PROMPT,
                "params": {
                    "sessionId": session_id,
                    "prompt": [text_block("hello")],
                },
            }
        )
        # EchoBackend streams one agent_message_chunk ("echo: hello") then ends.
        update = ws.receive_json()
        assert update["method"] == Method.SESSION_UPDATE
        assert update["params"]["update"]["content"]["text"] == "echo: hello"

        result = ws.receive_json()
        assert result["id"] == 3
        assert result["result"]["stopReason"] == StopReason.END_TURN
