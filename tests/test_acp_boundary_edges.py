"""Failure and transport-boundary coverage for ACP adapters."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from mac.acp import backend, executor, peer, ws_client
from mac.acp.protocol import JSONRPCError, RequestPermissionResult


class _FakeEvent:
    def __init__(self) -> None:
        self.flag = False
        self.waits = 0

    def wait(self, _timeout=None) -> bool:
        self.waits += 1
        return self.flag or self.waits > 1

    def set(self) -> None:
        self.flag = True

    def is_set(self) -> bool:
        return self.flag


class _InlineThread:
    def __init__(self, target=None, **_kwargs) -> None:
        self.target = target

    def start(self) -> None:
        if self.target:
            self.target()


class _Stdout:
    def __init__(self, lines) -> None:
        self.lines = list(lines)
        self.closed = False

    def __iter__(self):
        return iter(self.lines)

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def close(self) -> None:
        self.closed = True


class _Proc:
    def __init__(self, lines=("one\n", "two\n"), returncode=0) -> None:
        self.stdout = _Stdout(lines)
        self.stdin = SimpleNamespace(write=lambda data: setattr(self, "written", data), flush=lambda: setattr(self, "flushed", True))
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def poll(self):
        return None


def test_backend_default_and_argv_precedence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MAC_HERMES_PYTHON", "/custom/python")
    assert backend.default_argv("prompt")[0] == "/custom/python"
    explicit = backend.MacAgentBackend(argv=["agent", "run"])
    assert explicit._resolve_argv("prompt") == ["agent", "run", "prompt"]
    monkeypatch.setenv("MAC_ACP_BACKEND_CMD", "env-agent --flag")
    env_backend = backend.MacAgentBackend()
    assert env_backend._resolve_argv("prompt") == ["env-agent", "--flag", "prompt"]
    monkeypatch.delenv("MAC_ACP_BACKEND_CMD")
    assert env_backend._resolve_argv("prompt")[-2:] == ["prompt", "--quiet"]


def test_subprocess_runner_streams_and_closes(monkeypatch) -> None:
    proc = _Proc()
    monkeypatch.setattr(backend.subprocess, "Popen", lambda *_a, **_k: proc)
    monkeypatch.setattr(backend.threading, "Event", _FakeEvent)
    monkeypatch.setattr(backend.threading, "Thread", _InlineThread)
    seen = []
    assert backend._subprocess_runner(["agent"], ".", seen.append) == 0
    assert seen == ["", "one", "two"]
    assert proc.stdout.closed is True


def test_subprocess_runner_cancels_from_output_and_watcher(monkeypatch) -> None:
    for cancel_on in ("line", "watch"):
        proc = _Proc(["line\n"])
        monkeypatch.setattr(backend.subprocess, "Popen", lambda *_a, proc=proc, **_k: proc)
        monkeypatch.setattr(backend.threading, "Event", _FakeEvent)
        monkeypatch.setattr(backend.threading, "Thread", _InlineThread)

        def on_line(value):
            if (cancel_on == "line" and value == "line") or (cancel_on == "watch" and value == ""):
                raise backend._Cancelled()

        with pytest.raises(backend._Cancelled):
            backend._subprocess_runner(["agent"], ".", on_line)
        assert proc.terminated is True


def test_subprocess_watcher_ignores_callback_failure(monkeypatch) -> None:
    proc = _Proc([])
    monkeypatch.setattr(backend.subprocess, "Popen", lambda *_a, **_k: proc)
    monkeypatch.setattr(backend.threading, "Event", _FakeEvent)
    monkeypatch.setattr(backend.threading, "Thread", _InlineThread)

    def callback(value):
        if value == "":
            raise RuntimeError("observer failed")

    assert backend._subprocess_runner(["agent"], ".", callback) == 0


def test_terminate_escalates_to_kill() -> None:
    proc = _Proc()
    calls = 0

    def wait(timeout=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired("agent", timeout)
        return 0

    proc.wait = wait
    backend._terminate(proc)
    assert proc.terminated is True
    assert proc.killed is True


def test_acp_executor_runs_full_client_contract(monkeypatch) -> None:
    calls = []
    fake_peer = object()

    class FakeClient:
        def __init__(self, peer_value, **kwargs):
            calls.append(("init", peer_value, kwargs))

        def on_update(self, callback):
            self.update = callback

        def on_request_permission(self, callback):
            calls.append(("permission", callback(None)))

        def initialize(self, timeout=None):
            self.update({"event": "started"})
            return SimpleNamespace(protocol_version=1)

        def session_new(self, cwd, timeout=None):
            calls.append(("new", cwd, timeout))
            return "session"

        def session_prompt(self, session_id, prompt, timeout=None):
            calls.append(("prompt", session_id, prompt, timeout))
            return SimpleNamespace(stop_reason="end_turn")

    monkeypatch.setattr(executor, "ACPClient", FakeClient)
    updates = []
    run = executor.ACPExecutor(
        ["unused"], cwd="/work", peer_factory=lambda: fake_peer, client_info={"name": "test"}
    ).run(
        "hello",
        on_update=updates.append,
        on_permission=lambda _params: RequestPermissionResult(outcome="cancelled"),
        timeout=3,
    )
    assert run.session_id == "session"
    assert run.stop_reason == "end_turn"
    assert run.updates == updates == [{"event": "started"}]


def test_acp_executor_stdio_peer_is_closed(monkeypatch) -> None:
    closed = []
    stdio = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(executor, "stdio_peer", lambda argv: stdio)
    instance = executor.ACPExecutor(["agent"])
    assert instance._build_peer() == (stdio, stdio.close)

    class BrokenClient:
        def __init__(self, *_a, **_k):
            pass

        def on_update(self, _callback):
            raise RuntimeError("stop")

    monkeypatch.setattr(executor, "ACPClient", BrokenClient)
    with pytest.raises(RuntimeError, match="stop"):
        instance.run("prompt")
    assert closed == [True]


def test_pending_request_timeout_error_and_success() -> None:
    pending = peer.PendingRequest(1)
    with pytest.raises(TimeoutError):
        pending.result(0)
    pending._fulfill(SimpleNamespace(is_error=True, error=JSONRPCError(code=1, message="bad"), result=None))
    assert pending.done() is True
    with pytest.raises(peer.RemoteError, match="bad"):
        pending.result(0)
    success = peer.PendingRequest(2)
    success._fulfill(SimpleNamespace(is_error=False, error=None, result={"ok": True}))
    assert success.result(0) == {"ok": True}


def test_peer_request_error_deferred_and_notification_paths() -> None:
    sent = []
    endpoint = peer.Peer(sent.append)
    assert endpoint.feed_and_pump(b"\n") == 0
    endpoint.feed_and_pump(b'{"jsonrpc":"2.0","id":1,"method":"missing"}\n')
    assert "method not found" in sent[-1].decode()

    endpoint.on_request("explode", lambda _params: (_ for _ in ()).throw(RuntimeError("boom")))
    endpoint.feed_and_pump(b'{"jsonrpc":"2.0","id":2,"method":"explode"}\n')
    assert "boom" in sent[-1].decode()

    endpoint.on_request(
        "remote",
        lambda _params: (_ for _ in ()).throw(peer.RemoteError(JSONRPCError(code=9, message="wire"))),
    )
    endpoint.feed_and_pump(b'{"jsonrpc":"2.0","id":3,"method":"remote"}\n')
    assert "wire" in sent[-1].decode()

    before = len(sent)
    endpoint.on_request_raw("later", lambda _request: peer.DEFERRED)
    endpoint.feed_and_pump(b'{"jsonrpc":"2.0","id":4,"method":"later"}\n')
    assert len(sent) == before
    endpoint.respond_error(4, JSONRPCError(code=5, message="later error"))
    assert "later error" in sent[-1].decode()

    notices = []
    endpoint.on_notification("notice", notices.append)
    endpoint.feed_and_pump(b'{"jsonrpc":"2.0","method":"notice","params":{"x":1}}\n')
    assert notices == [{"x": 1}]


def test_stdio_peer_transport_lifecycle(monkeypatch) -> None:
    proc = _Proc([b'{"jsonrpc":"2.0","method":"notice"}\n'])
    thread = SimpleNamespace(start=lambda: None)
    monkeypatch.setattr(peer.subprocess, "Popen", lambda *_a, **_k: proc)
    monkeypatch.setattr(peer.threading, "Thread", lambda **_k: thread)
    stdio = peer._StdioPeer(["agent"])
    stdio._send_to_stdin(b"hello")
    assert proc.written == b"hello"
    assert proc.flushed is True
    stdio._read_loop()
    stdio.close()
    assert proc.terminated is True

    proc2 = _Proc([])
    proc2.wait = lambda timeout=None: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", timeout))
    monkeypatch.setattr(peer.subprocess, "Popen", lambda *_a, **_k: proc2)
    stdio2 = peer._StdioPeer(["agent"])
    stdio2.close()
    assert proc2.killed is True


class _Connection:
    def __init__(self, messages=()) -> None:
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(value)

    def recv(self):
        if not self.messages:
            return None
        value = self.messages.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


def test_websocket_dial_headers_and_subprotocols(monkeypatch) -> None:
    connection = _Connection()
    captured = {}
    websocket = SimpleNamespace(
        create_connection=lambda url, **kwargs: captured.update(url=url, kwargs=kwargs) or connection
    )
    monkeypatch.setattr(ws_client, "_load_websocket", lambda: websocket)
    monkeypatch.setattr(ws_client.threading, "Thread", lambda **_k: SimpleNamespace(start=lambda: None))
    client_peer = ws_client.connect_acp_websocket(
        "ws://host/acp", token="tok", header={"X-Test": "yes"}, timeout=4
    )
    assert captured["url"] == "ws://host/acp"
    assert "Authorization: Bearer tok" in captured["kwargs"]["header"]
    assert captured["kwargs"]["subprotocols"] == ["Authorization", "tok"]
    client_peer.close()
    assert connection.closed is True
    assert ws_client._normalize_headers(None) == []
    assert ws_client._normalize_headers({"A": 1}) == ["A: 1"]
    assert ws_client._normalize_headers(["B: 2"]) == ["B: 2"]


def test_websocket_peer_send_read_and_close_failures(monkeypatch) -> None:
    monkeypatch.setattr(ws_client.threading, "Thread", lambda **_k: SimpleNamespace(start=lambda: None))
    connection = _Connection([b'{"jsonrpc":"2.0","method":"n"}', '{"jsonrpc":"2.0","method":"n"}', OSError("closed")])
    endpoint = ws_client._WebSocketPeer(connection)
    endpoint._send_to_socket(b"frame\n")
    assert connection.sent == ["frame"]
    endpoint._read_loop()
    endpoint._stop.set()
    endpoint._send_to_socket(b"ignored\n")
    assert connection.sent == ["frame"]

    connection.close = lambda: (_ for _ in ()).throw(OSError("close failed"))
    endpoint.close()


def test_websocket_client_wrapper_proxies_and_closes(monkeypatch) -> None:
    closed = []
    fake_peer = SimpleNamespace(close=lambda: closed.append(True))
    fake_client = SimpleNamespace(initialize=lambda: "initialized")
    monkeypatch.setattr(ws_client, "connect_acp_websocket", lambda *_a, **_k: fake_peer)
    monkeypatch.setattr(ws_client, "ACPClient", lambda *_a, **_k: fake_client)
    with ws_client.ACPWebSocketClient(connection=object()) as wrapped:
        assert wrapped.peer is fake_peer
        assert wrapped.initialize() == "initialized"
    assert closed == [True]
