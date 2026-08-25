"""End-to-end ACPAgentServer test driven by the real ACPClient.

The two endpoints -- mac's :class:`ACPAgentServer` (the agent/server role) and
mac's :class:`ACPClient` (the host/client role) -- are wired over an in-memory
**paired transport**: each peer's ``send`` enqueues bytes into the other peer's
inbox. A daemon thread continuously pumps both peers (``run_background_pump``),
so the full server/client round-trip runs synchronously from the test's point of
view with no asyncio, no subprocess, and no third-party deps.

A background pump (rather than a single steady-state ``drive``) is required
because the server's prompt handler legitimately *blocks* mid-turn: it issues a
``session/request_permission`` request to the client and waits for the decision.
That handler runs on the thread that pumped the inbound ``session/prompt`` frame,
so the response (and any ``session/cancel``) must be pumped from a *different*
thread, or the single-threaded pump would deadlock against itself.

The backend used here is a scripted :class:`PromptBackend` that streams two
``session/update`` notifications (an agent message chunk + a tool call), asks the
client for permission, records the decision, then ends the turn -- mirroring the
shape of a real prompt turn.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from mac.acp.client import ACPClient
from mac.acp.peer import Peer, RemoteError
from mac.acp.protocol import (
    AgentCapabilities,
    InitializeResult,
    Method,
    PermissionOption,
    PromptResult,
    RequestPermissionParams,
    RequestPermissionResult,
    SessionUpdateKind,
    StopReason,
)
from mac.acp.server import ACPAgentServer, PromptTurn

# imports relocated from test_acp_server_edges.py
import io
from types import SimpleNamespace
import pytest
from mac.acp import server
from mac.acp.peer import RemoteError
from mac.acp.protocol import AgentCapabilities, JSONRPCError, PermissionOption, StopReason


class _Pipe:
    """A pair of peers connected to each other's inbox.

    The peers are pumped by a daemon thread started via
    :meth:`run_background_pump`; the returned stop event halts it. Driving the
    pump off the test's main thread is what lets a blocking server handler (one
    that waits on a client request mid-turn) make progress without deadlock.
    """

    def __init__(self) -> None:
        self.client_peer = Peer(send=self._to_server)
        self.server_peer = Peer(send=self._to_client)

    def _to_server(self, data: bytes) -> None:
        self.server_peer.feed(data)

    def _to_client(self, data: bytes) -> None:
        self.client_peer.feed(data)

    def run_background_pump(self) -> "threading.Event":
        """Continuously pump both peers on a daemon thread until stopped.

        Driving the pump off the test's main thread lets blocking client calls
        (``request().result()``) make progress. The server runs each prompt turn
        on its *own* worker thread (its prompt handler returns immediately), so a
        single pump thread never gets stuck inside a blocking handler -- it stays
        free to deliver the permission response and any ``session/cancel`` that
        arrive while the turn is in flight.
        """

        stop = threading.Event()

        def _loop() -> None:
            while not stop.is_set():
                processed = self.server_peer.pump() + self.client_peer.pump()
                if processed == 0:
                    stop.wait(0.001)

        threading.Thread(target=_loop, daemon=True).start()
        return stop


class ScriptedBackend:
    """A scripted :class:`~mac.acp.server.PromptBackend`.

    During a turn it streams an ``agent_message_chunk`` and a ``tool_call``,
    requests permission from the client, records the decision, then ends the
    turn. Captures the turns it ran (and the permission decision) for assertions.
    """

    def __init__(self) -> None:
        self.turns: List[PromptTurn] = []
        self.decisions: List[RequestPermissionResult] = []

    def run_prompt(self, turn: PromptTurn) -> str:
        self.turns.append(turn)
        turn.agent_message_chunk("working on it")
        turn.tool_call("call_1", "run shell", kind="execute", status="pending")
        decision = turn.request_permission(
            tool_call={"toolCallId": "call_1", "title": "run shell"},
            options=[
                PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
                PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
            ],
            timeout=5,
        )
        self.decisions.append(decision)
        return StopReason.END_TURN


def test_full_session_with_updates_and_permission():
    pipe = _Pipe()
    backend = ScriptedBackend()
    server = ACPAgentServer(
        pipe.server_peer,
        backend,
        agent_capabilities=AgentCapabilities(
            load_session=False,
            prompt_capabilities={"image": True},
        ),
        agent_info={"name": "mac-test-agent", "version": "9"},
    )
    client = ACPClient(pipe.client_peer)

    received_updates: List[Dict[str, Any]] = []
    client.on_update(lambda update: received_updates.append(update))

    permission_calls: List[RequestPermissionParams] = []

    def _permit(req: RequestPermissionParams) -> RequestPermissionResult:
        permission_calls.append(req)
        return RequestPermissionResult(outcome="selected", option_id="allow")

    client.on_request_permission(_permit)

    stop = pipe.run_background_pump()
    try:
        # --- initialize negotiates v1 and surfaces the server's capabilities ---
        init = client.initialize(timeout=5)
        assert isinstance(init, InitializeResult)
        assert init.protocol_version == 1
        assert init.agent_capabilities.load_session is False
        assert init.agent_capabilities.prompt_capabilities == {"image": True}
        assert init.agent_info == {"name": "mac-test-agent", "version": "9"}

        # --- session/new allocates and returns an id ---
        session_id = client.session_new(cwd="/tmp/work", timeout=5)
        assert session_id
        assert session_id in server.session_ids()

        # --- session/prompt drives the backend (updates + permission) ---
        result = client.session_prompt(session_id, "do the thing", timeout=5)
        assert isinstance(result, PromptResult)
        assert result.stop_reason == StopReason.END_TURN
    finally:
        stop.set()

    # The backend ran exactly one turn, carrying the session id, cwd, and prompt.
    assert len(backend.turns) == 1
    turn = backend.turns[0]
    assert turn.session_id == session_id
    assert turn.cwd == "/tmp/work"
    assert turn.content == [{"type": "text", "text": "do the thing"}]

    # The client's update handler saw both streamed notifications, in order.
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

    # The permission request round-tripped to the client handler...
    assert len(permission_calls) == 1
    assert permission_calls[0].session_id == session_id
    assert permission_calls[0].tool_call["toolCallId"] == "call_1"
    assert [o.option_id for o in permission_calls[0].options] == ["allow", "reject"]

    # ...and the backend received the client's "selected/allow" decision.
    assert len(backend.decisions) == 1
    assert backend.decisions[0].outcome == "selected"
    assert backend.decisions[0].option_id == "allow"


def test_initialize_negotiates_down_to_client_version():
    """The agent answers no higher than the client requested (and never above
    its own pinned version)."""

    pipe = _Pipe()
    ACPAgentServer(pipe.server_peer, lambda turn: StopReason.END_TURN)
    client = ACPClient(pipe.client_peer)

    stop = pipe.run_background_pump()
    try:
        init = client.initialize(timeout=5)
    finally:
        stop.set()
    # The protocol pins v1, so v1 in -> v1 out. (Forward-compat guard: when the
    # vendored version bumps, the server's min() still clamps to the client's.)
    assert init.protocol_version == 1


def test_session_load_rejected_when_unsupported():
    """With loadSession=false (default), session/load is rejected as a wire
    error rather than silently succeeding."""

    pipe = _Pipe()
    ACPAgentServer(pipe.server_peer, lambda turn: StopReason.END_TURN)

    stop = pipe.run_background_pump()
    try:
        pending = pipe.client_peer.request(
            Method.SESSION_LOAD, {"sessionId": "sess_missing", "cwd": "/tmp"}
        )
        err: Optional[RemoteError] = None
        try:
            pending.result(timeout=5)
        except RemoteError as exc:
            err = exc
    finally:
        stop.set()

    assert err is not None
    assert "session/load" in err.error.message


def test_prompt_for_unknown_session_errors():
    """A prompt against an unallocated session id is a wire-level error."""

    pipe = _Pipe()
    ACPAgentServer(pipe.server_peer, lambda turn: StopReason.END_TURN)
    client = ACPClient(pipe.client_peer)

    stop = pipe.run_background_pump()
    try:
        err: Optional[RemoteError] = None
        try:
            client.session_prompt("sess_nope", "hi", timeout=5)
        except RemoteError as exc:
            err = exc
    finally:
        stop.set()

    assert err is not None
    assert "unknown session" in err.error.message


def test_session_cancel_sets_turn_cancelled_flag():
    """An inbound session/cancel notification flips the live turn's ``cancelled``
    flag, which the backend polls mid-turn to stop early."""

    pipe = _Pipe()
    seen: Dict[str, Any] = {}
    turn_started = threading.Event()
    cancel_seen = threading.Event()

    def _backend(turn: PromptTurn) -> str:
        seen["before"] = turn.cancelled
        turn_started.set()
        # Poll the flag until the test injects session/cancel.
        for _ in range(500):
            if turn.cancelled:
                break
            threading.Event().wait(0.01)
        if turn.cancelled:
            cancel_seen.set()
        seen["after"] = turn.cancelled
        return StopReason.CANCELLED if turn.cancelled else StopReason.END_TURN

    ACPAgentServer(pipe.server_peer, _backend)
    client = ACPClient(pipe.client_peer)

    stop = pipe.run_background_pump()
    try:
        session_id = client.session_new(cwd="/tmp/work", timeout=5)

        box: Dict[str, Any] = {}

        def _worker():
            box["value"] = client.session_prompt(session_id, "do the thing", timeout=5)

        t = threading.Thread(target=_worker)
        t.start()

        assert turn_started.wait(timeout=5), "backend never started the turn"
        assert seen.get("before") is False

        # Inject the cancel; the background pump runs the notification handler,
        # which flips the live turn's flag, releasing the backend's poll loop.
        client.cancel(session_id)
        assert cancel_seen.wait(timeout=5), "turn never observed cancellation"

        t.join(timeout=5)
        assert not t.is_alive(), "prompt did not complete"
    finally:
        stop.set()

    assert seen.get("after") is True
    assert box["value"].stop_reason == StopReason.CANCELLED


# --- relocated from test_acp_server_edges.py (coverage companion folded in) ---


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
    monkeypatch.setattr(
        server,
        "ACPAgentServer",
        lambda peer, backend, **kwargs: SimpleNamespace(
            wait_idle=lambda timeout: captured.append(type(backend))
        ),
    )
    server.serve_stdio(stdin=io.BytesIO(b""), stdout=io.BytesIO())
    assert captured == [server.EchoBackend]
    monkeypatch.setattr(server, "serve_stdio", lambda: captured.append("main"))
    assert server.main([]) == 0
    assert captured[-1] == "main"
