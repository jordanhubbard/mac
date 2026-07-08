"""Tests for :class:`~mac.acp.backend.MacAgentBackend`.

Two layers, both subprocess-free via the injected ``runner`` seam:

* **Unit** -- drive :meth:`MacAgentBackend.run_prompt` against a fake
  :class:`PromptTurn` capture, asserting it streams the runner's lines as
  ``agent_message_chunk``\\ s and maps returncode -> stop reason
  (0 -> end_turn, nonzero -> refusal), plus the cancellation path.
* **End-to-end** -- a real :class:`ACPClient` <-> real
  :class:`ACPAgentServer(MacAgentBackend(runner=fake))` over the in-memory paired
  transport (the harness mirrors ``tests/test_acp_server.py``): the client
  prompts, the fake runner's lines arrive at the client as
  ``agent_message_chunk`` updates, and the prompt returns ``end_turn``.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List

from mac.acp.backend import MacAgentBackend, default_argv
from mac.acp.client import ACPClient
from mac.acp.peer import Peer
from mac.acp.protocol import (
    PromptResult,
    SessionUpdateKind,
    StopReason,
)
from mac.acp.server import ACPAgentServer, PromptTurn


# ---------------------------------------------------------------------------
# Unit-level helpers: a PromptTurn that captures chunks without a live peer.
# ---------------------------------------------------------------------------


class _CaptureTurn(PromptTurn):
    """A :class:`PromptTurn` whose ``send_update`` records instead of sending.

    Lets the unit tests drive ``run_prompt`` without a real :class:`Peer` /
    client, while still exercising the genuine ``agent_message_chunk`` path.
    """

    def __init__(self, prompt: str = "do the thing", *, cwd: str = "/tmp/work") -> None:
        super().__init__(
            peer=None,  # never touched: send_update is overridden
            session_id="sess_1",
            content=[{"type": "text", "text": prompt}],
            cwd=cwd,
        )
        self.updates: List[Dict[str, Any]] = []

    def send_update(self, update: Dict[str, Any]) -> None:  # type: ignore[override]
        self.updates.append(dict(update))

    @property
    def chunks(self) -> List[str]:
        """The text of every ``agent_message_chunk`` streamed, in order."""

        return [
            u["content"]["text"]
            for u in self.updates
            if u.get("sessionUpdate") == SessionUpdateKind.AGENT_MESSAGE_CHUNK
        ]


def _fake_runner(lines, returncode):
    """A fake :data:`RunnerFn`: emit ``lines`` then return ``returncode``.

    Captures the ``argv`` and ``cwd`` it was handed for assertions.
    """

    captured: Dict[str, Any] = {}

    def runner(argv, cwd, on_line):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        for line in lines:
            on_line(line)
        return returncode

    runner.captured = captured  # type: ignore[attr-defined]
    return runner


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_run_prompt_streams_lines_and_ends_turn_on_zero():
    runner = _fake_runner(["line one", "line two"], 0)
    backend = MacAgentBackend(argv=["my-agent", "--flag"], runner=runner)
    turn = _CaptureTurn("hello agent", cwd="/work/dir")

    stop = backend.run_prompt(turn)

    assert stop == StopReason.END_TURN
    assert turn.chunks == ["line one", "line two"]
    # The runner saw the resolved argv (template + prompt appended) and the cwd.
    assert runner.captured["argv"] == ["my-agent", "--flag", "hello agent"]
    assert runner.captured["cwd"] == "/work/dir"


def test_run_prompt_nonzero_returncode_maps_to_refusal():
    runner = _fake_runner(["partial output"], 3)
    backend = MacAgentBackend(argv=["my-agent"], runner=runner)
    turn = _CaptureTurn()

    stop = backend.run_prompt(turn)

    assert stop == StopReason.REFUSAL
    # The streamed output plus a trailing failure note.
    assert turn.chunks[0] == "partial output"
    assert "exited with code 3" in turn.chunks[-1]


def test_run_prompt_empty_lines_are_not_streamed():
    runner = _fake_runner(["", "real", ""], 0)
    backend = MacAgentBackend(argv=["my-agent"], runner=runner)
    turn = _CaptureTurn()

    stop = backend.run_prompt(turn)

    assert stop == StopReason.END_TURN
    assert turn.chunks == ["real"]


def test_run_prompt_already_cancelled_short_circuits():
    called = {"ran": False}

    def runner(argv, cwd, on_line):
        called["ran"] = True
        return 0

    backend = MacAgentBackend(argv=["my-agent"], runner=runner)
    turn = _CaptureTurn()
    turn._mark_cancelled()

    stop = backend.run_prompt(turn)

    assert stop == StopReason.CANCELLED
    assert called["ran"] is False  # never spawned the runner


def test_run_prompt_cancelled_mid_run_returns_cancelled():
    """A turn cancelled while the runner streams ends as CANCELLED.

    The fake runner flips the turn's cancel flag partway through and keeps
    emitting; ``run_prompt`` must report CANCELLED regardless of the (here 0)
    return code, and not stream output produced after the cancel.
    """

    def runner(argv, cwd, on_line):
        on_line("before cancel")
        # Simulate the client's session/cancel landing mid-run.
        turn._mark_cancelled()
        on_line("after cancel")  # guarded out by run_prompt's on_line
        return 0

    backend = MacAgentBackend(argv=["my-agent"], runner=runner)
    turn = _CaptureTurn()

    stop = backend.run_prompt(turn)

    assert stop == StopReason.CANCELLED
    assert turn.chunks == ["before cancel"]


def test_default_argv_shape():
    """The default enters the verified OpenClaw OpenShell service sandbox."""

    argv = default_argv("solve it")
    assert argv[0].endswith("/.mac/bin/openclaw-agent")
    assert argv[1:] == [
        "--agent",
        "main",
        "--message",
        "solve it",
        "--session-id",
        "mac-acp",
        "--json",
    ]


# ---------------------------------------------------------------------------
# End-to-end: real client <-> real server(MacAgentBackend(runner=fake))
# ---------------------------------------------------------------------------


class _Pipe:
    """A pair of peers connected to each other's inbox (cf. test_acp_server).

    A daemon thread pumps both peers so a blocking client call makes progress;
    the server runs each prompt turn on its own worker thread.
    """

    def __init__(self) -> None:
        self.client_peer = Peer(send=self._to_server)
        self.server_peer = Peer(send=self._to_client)

    def _to_server(self, data: bytes) -> None:
        self.server_peer.feed(data)

    def _to_client(self, data: bytes) -> None:
        self.client_peer.feed(data)

    def run_background_pump(self) -> "threading.Event":
        stop = threading.Event()

        def _loop() -> None:
            while not stop.is_set():
                processed = self.server_peer.pump() + self.client_peer.pump()
                if processed == 0:
                    stop.wait(0.001)

        threading.Thread(target=_loop, daemon=True).start()
        return stop


def test_end_to_end_client_drives_mac_agent_backend():
    pipe = _Pipe()

    captured: Dict[str, Any] = {}

    def fake_runner(argv, cwd, on_line):
        captured["argv"] = list(argv)
        captured["cwd"] = cwd
        on_line("agent: thinking")
        on_line("agent: done")
        return 0

    backend = MacAgentBackend(argv=["mac-agent"], runner=fake_runner)
    server = ACPAgentServer(pipe.server_peer, backend)
    client = ACPClient(pipe.client_peer)

    received: List[Dict[str, Any]] = []
    client.on_update(lambda update: received.append(update))

    stop = pipe.run_background_pump()
    try:
        client.initialize(timeout=5)
        session_id = client.session_new(cwd="/tmp/agent-cwd", timeout=5)
        assert session_id in server.session_ids()

        result = client.session_prompt(session_id, "build the thing", timeout=5)
        assert isinstance(result, PromptResult)
        assert result.stop_reason == StopReason.END_TURN
    finally:
        stop.set()

    # The fake runner ran in the session cwd with the prompt appended to argv.
    assert captured["cwd"] == "/tmp/agent-cwd"
    assert captured["argv"] == ["mac-agent", "build the thing"]

    # Both runner lines arrived at the client as agent_message_chunk updates.
    chunks = [
        u["update"]["content"]["text"]
        for u in received
        if u["update"].get("sessionUpdate") == SessionUpdateKind.AGENT_MESSAGE_CHUNK
    ]
    assert chunks == ["agent: thinking", "agent: done"]
    assert all(u["sessionId"] == session_id for u in received)


def test_end_to_end_nonzero_returncode_refuses():
    pipe = _Pipe()

    def fake_runner(argv, cwd, on_line):
        on_line("agent: failing")
        return 7

    backend = MacAgentBackend(argv=["mac-agent"], runner=fake_runner)
    ACPAgentServer(pipe.server_peer, backend)
    client = ACPClient(pipe.client_peer)

    received: List[Dict[str, Any]] = []
    client.on_update(lambda update: received.append(update))

    stop = pipe.run_background_pump()
    try:
        client.initialize(timeout=5)
        session_id = client.session_new(cwd="/tmp", timeout=5)
        result = client.session_prompt(session_id, "do it", timeout=5)
        assert result.stop_reason == StopReason.REFUSAL
    finally:
        stop.set()

    chunks = [
        u["update"]["content"]["text"]
        for u in received
        if u["update"].get("sessionUpdate") == SessionUpdateKind.AGENT_MESSAGE_CHUNK
    ]
    assert chunks[0] == "agent: failing"
    assert "exited with code 7" in chunks[-1]
