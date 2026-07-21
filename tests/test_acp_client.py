"""End-to-end ACPClient test driven by a fake in-process ACP agent.

The two endpoints (mac's :class:`ACPClient` and a scripted fake agent) are wired
over an in-memory **paired transport**: each peer's ``send`` enqueues bytes into
a shared buffer, and a small ``drive`` loop pumps both peers until their inboxes
drain. This exercises the full peer/client stack synchronously with no asyncio,
no subprocess, and no third-party deps.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from mac.acp.client import ACPClient
from mac.acp.peer import Peer
from mac.acp.protocol import (
    AgentCapabilities,
    InitializeResult,
    Method,
    NewSessionResult,
    PromptResult,
    RequestPermissionParams,
    RequestPermissionResult,
    SessionUpdateKind,
    StopReason,
    text_block,
)


class _Pipe:
    """A pair of peers connected to each other's inbox, pumpable in a loop."""

    def __init__(self) -> None:
        self.activity = threading.Event()
        self.client_peer = Peer(send=self._to_agent)
        self.agent_peer = Peer(send=self._to_client)

    def _to_agent(self, data: bytes) -> None:
        self.agent_peer.feed(data)
        self.activity.set()

    def _to_client(self, data: bytes) -> None:
        self.client_peer.feed(data)
        self.activity.set()

    def drive(self, rounds: int = 10) -> None:
        """Pump both peers until neither processes a frame (steady state)."""

        for _ in range(rounds):
            processed = self.agent_peer.pump() + self.client_peer.pump()
            if processed == 0:
                break


class FakeAgent:
    """A minimal scripted ACP agent: answers the baseline methods and emits a
    couple of ``session/update`` notifications during the prompt turn, then asks
    the client for permission before finishing."""

    def __init__(self, peer: Peer) -> None:
        self._peer = peer
        self.permission_result: Optional[RequestPermissionResult] = None
        peer.on_request(Method.INITIALIZE, self._initialize)
        peer.on_request(Method.SESSION_NEW, self._session_new)
        peer.on_request(Method.SESSION_PROMPT, self._session_prompt)

    def _initialize(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return InitializeResult(
            agent_capabilities=AgentCapabilities(load_session=True),
            agent_info={"name": "fake-agent", "version": "1"},
        ).to_dict()

    def _session_new(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return NewSessionResult(session_id="sess_fake_1").to_dict()

    def _session_prompt(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        session_id = (params or {})["sessionId"]
        # Stream two update notifications back to the client mid-turn.
        self._peer.notify(
            Method.SESSION_UPDATE,
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                    "content": text_block("working on it"),
                },
            },
        )
        self._peer.notify(
            Method.SESSION_UPDATE,
            {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": SessionUpdateKind.TOOL_CALL,
                    "toolCallId": "call_1",
                    "title": "run shell",
                },
            },
        )
        # Ask the client for permission; the response is correlated by the peer.
        pending = self._peer.request(
            Method.SESSION_REQUEST_PERMISSION,
            RequestPermissionParams(
                session_id=session_id,
                tool_call={"toolCallId": "call_1", "title": "run shell"},
                options=[],
            ).to_dict(),
        )
        # We don't block here (single-threaded test); record the decision once
        # the client answers. The pending future resolves during ``drive``.
        self._pending_permission = pending
        return PromptResult(stop_reason=StopReason.END_TURN).to_dict()


def test_full_session_with_updates_and_permission():
    pipe = _Pipe()
    agent = FakeAgent(pipe.agent_peer)
    client = ACPClient(pipe.client_peer)

    received_updates: List[Dict[str, Any]] = []
    client.on_update(lambda update: received_updates.append(update))

    permission_calls: List[RequestPermissionParams] = []

    def _permit(req: RequestPermissionParams) -> RequestPermissionResult:
        permission_calls.append(req)
        return RequestPermissionResult(
            outcome="selected", option_id="allow"
        )

    client.on_request_permission(_permit)

    # --- initialize ---
    init = _call(pipe, lambda: client.initialize())
    assert isinstance(init, InitializeResult)
    assert init.protocol_version == 1
    assert init.agent_capabilities.load_session is True

    # --- session/new ---
    session_id = _call(pipe, lambda: client.session_new(cwd="/tmp/work"))
    assert session_id == "sess_fake_1"

    # --- session/prompt (drives updates + a permission round-trip) ---
    result = _call(pipe, lambda: client.session_prompt(session_id, "do the thing"))
    assert isinstance(result, PromptResult)
    assert result.stop_reason == StopReason.END_TURN

    # The update handler saw both streamed notifications.
    kinds = [u["update"]["sessionUpdate"] for u in received_updates]
    assert kinds == [
        SessionUpdateKind.AGENT_MESSAGE_CHUNK,
        SessionUpdateKind.TOOL_CALL,
    ]

    # The permission request round-tripped to the client handler...
    assert len(permission_calls) == 1
    assert permission_calls[0].session_id == session_id
    assert permission_calls[0].tool_call["toolCallId"] == "call_1"

    # ...and the agent received the client's "selected/allow" decision.
    decision = RequestPermissionResult.from_dict(
        agent._pending_permission.result(timeout=0)
    )
    assert decision.outcome == "selected"
    assert decision.option_id == "allow"


def _call(pipe: _Pipe, fn):
    """Run a blocking client call against the synchronous paired transport.

    The client's ``request`` returns a PendingRequest whose ``result`` blocks; we
    instead issue the request, drive both peers to steady state so the response
    is delivered, then read the (now-resolved) result with a zero timeout. We do
    this by monkey-poking: the client methods call ``.result()`` internally, so
    we drive the pipe on a background-free trick -- issue then pump.

    Implementation: the client methods block on ``PendingRequest.result``. To
    keep everything single-threaded we run the call on a tiny worker thread and
    pump the pipe from the main thread until it returns.
    """

    box: Dict[str, Any] = {}
    completed = threading.Event()

    def _worker():
        try:
            box["value"] = fn()
        except BaseException as exc:  # surface worker failures on the test thread
            box["error"] = exc
        finally:
            completed.set()

    pipe.activity.clear()
    t = threading.Thread(target=_worker)
    t.start()
    # Wait for the request frame instead of racing a fixed number of pump
    # iterations against thread scheduling. Once the frame exists, drive() is
    # synchronous and drains the complete request/response exchange.
    assert pipe.activity.wait(timeout=30), "client did not publish a request frame"
    pipe.activity.clear()
    pipe.drive()
    assert completed.wait(timeout=30), "client call deadlocked after response delivery"
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]
