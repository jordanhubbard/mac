"""Stdio and error-boundary coverage for the ACP agent server."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from mac.acp import server
from mac.acp.peer import RemoteError
from mac.acp.protocol import AgentCapabilities, JSONRPCError, PermissionOption, StopReason


class _Peer:
    def __init__(self):
        self.notifications = []
        self.results = []
        self.errors = []
        self.pending = SimpleNamespace(result=lambda timeout: {})

    def notify(self, method, params):
        self.notifications.append((method, params))

    def request(self, method, params):
        return self.pending

    def on_request(self, *_a):
        pass

    def on_request_raw(self, *_a):
        pass

    def on_notification(self, *_a):
        pass

    def respond_result(self, request_id, result):
        self.results.append((request_id, result))

    def respond_error(self, request_id, error):
        self.errors.append((request_id, error))


def test_prompt_turn_thought_tool_update_and_permission_error() -> None:
    peer = _Peer()
    turn = server.PromptTurn(peer, "session", [], permission_timeout=3)
    turn.agent_thought_chunk("thinking")
    turn.tool_call_update("call", status="done", result={"ok": True})
    assert peer.notifications[0][1]["update"]["sessionUpdate"] == "agent_thought_chunk"
    assert peer.notifications[1][1]["update"]["status"] == "done"
    peer.pending = SimpleNamespace(
        result=lambda _timeout: (_ for _ in ()).throw(
            RemoteError(JSONRPCError(code=-1, message="unsupported"))
        )
    )
    result = turn.request_permission(
        {"toolCallId": "call"}, [PermissionOption("allow", "Allow", "allow_once")]
    )
    assert result.outcome == "cancelled"


def test_backend_normalization_echo_and_session_load_paths() -> None:
    with pytest.raises(TypeError, match="backend must"):
        server._as_backend(3)
    peer = _Peer()
    backend = server._as_backend(lambda _turn: StopReason.END_TURN)
    assert backend.run_prompt(SimpleNamespace()) == StopReason.END_TURN
    turn = server.PromptTurn(peer, "session", [{"type": "text", "text": "hi"}])
    turn._mark_cancelled()
    assert server.EchoBackend().run_prompt(turn) == StopReason.CANCELLED

    acp = server.ACPAgentServer(
        peer,
        lambda _turn: StopReason.END_TURN,
        agent_capabilities=AgentCapabilities(load_session=True),
    )
    with pytest.raises(RemoteError, match="session/load not implemented"):
        acp._handle_session_load({"sessionId": "s", "cwd": "/tmp"})
    with pytest.raises(Exception):
        acp._handle_authenticate({})


def test_run_turn_remote_and_internal_errors() -> None:
    peer = _Peer()
    session = SimpleNamespace(current_turn=None)
    acp = server.ACPAgentServer(peer, lambda _turn: StopReason.END_TURN)
    turn = server.PromptTurn(peer, "session", [])
    session.current_turn = turn
    acp._active_turns = 1
    acp._backend = SimpleNamespace(
        run_prompt=lambda _turn: (_ for _ in ()).throw(
            RemoteError(JSONRPCError(code=-1, message="remote"))
        )
    )
    acp._run_turn(1, session, turn)
    assert peer.errors[-1][1].message == "remote"
    assert session.current_turn is None and acp._active_turns == 0

    turn = server.PromptTurn(peer, "session", [])
    session.current_turn = turn
    acp._active_turns = 1
    acp._backend = SimpleNamespace(
        run_prompt=lambda _turn: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    acp._run_turn(2, session, turn)
    assert peer.errors[-1][1].message == "boom"


def test_serve_stdio_pumps_bytes_and_text_and_flushes(monkeypatch) -> None:
    events = []

    class FakePeer:
        def __init__(self, send):
            self.send = send

        def feed_and_pump(self, data):
            events.append(data)
            self.send(b"response\n")

    class FakeServer:
        def __init__(self, peer, backend, **kwargs):
            events.append((backend, kwargs))

        def wait_idle(self, timeout):
            events.append(("idle", timeout))

    class Input:
        def __init__(self):
            self.lines = iter([b"one\n", "two\n", b""])

        def readline(self):
            return next(self.lines)

    class Output(io.BytesIO):
        def flush(self):
            events.append("flush")

    monkeypatch.setattr(server, "Peer", FakePeer)
    monkeypatch.setattr(server, "ACPAgentServer", FakeServer)
    output = Output()
    server.serve_stdio(lambda _turn: StopReason.END_TURN, stdin=Input(), stdout=output, marker=True)
    assert [event for event in events if isinstance(event, bytes)][:2] == [b"one\n", b"two\n"]
    assert output.getvalue() == b"response\nresponse\n"
    assert ("idle", 5) in events


def test_serve_stdio_default_backend_and_main(monkeypatch) -> None:
    captured = []
    monkeypatch.delenv("MAC_ACP_BACKEND_CMD", raising=False)
    monkeypatch.setattr(server, "Peer", lambda send: SimpleNamespace())
    monkeypatch.setattr(server, "ACPAgentServer", lambda peer, backend, **kwargs: SimpleNamespace(wait_idle=lambda timeout: captured.append(type(backend))))
    server.serve_stdio(stdin=io.BytesIO(b""), stdout=io.BytesIO())
    assert captured == [server.EchoBackend]
    monkeypatch.setattr(server, "serve_stdio", lambda: captured.append("main"))
    assert server.main([]) == 0
    assert captured[-1] == "main"
