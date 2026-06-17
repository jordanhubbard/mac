"""Tests for the outbound A2A (Agent2Agent) client.

Drives :class:`mac.a2a.client.A2AClient` against the *real* inbound server
in-process via an injected transport -- no socket. The fake transport's
``post("/a2a", body)`` forwards straight to :meth:`A2AService.handle_rpc`
(which already returns a JSON-RPC result/error envelope), and its ``get(path)``
returns the AgentCard, so the full client round-trip exercises the same code an
external peer would hit over HTTP. A single stdlib-mock test confirms the
default :class:`~mac.a2a.client._UrllibTransport` builds a bearer-authenticated
JSON-RPC POST.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping

import pytest

from mac.a2a import A2AClient, A2AClientError
from mac.a2a.card import agent_card
from mac.a2a.client import _NotFound, _UrllibTransport
from mac.a2a.protocol import TaskState
from mac.a2a.service import A2AService
from mac.models import TaskState as MacTaskState
from mac.services import ControlPlane


# -- in-process transport wired to the real inbound service -----------------


class _InProcessTransport:
    """Routes client calls to a real :class:`A2AService` with no I/O.

    ``post`` unwraps the JSON-RPC request and calls ``handle_rpc`` (returning
    the envelope verbatim). ``get`` serves the AgentCard at the canonical path
    and, by default, 404s the legacy path so the fallback is testable; flip
    ``card_path`` to simulate an agent that only serves the legacy alias.
    """

    def __init__(self, service: A2AService, *, base_url: str, card_path: str | None = None) -> None:
        self.service = service
        self.base_url = base_url
        # When set, only this well-known path returns the card; others 404.
        self.card_path = card_path
        self.posted: list[Dict[str, Any]] = []

    def post(self, path: str, json_body: Mapping[str, Any]) -> Dict[str, Any]:
        assert path == "/a2a", path
        self.posted.append(dict(json_body))
        return self.service.handle_rpc(
            json_body["method"], json_body.get("params"), json_body.get("id")
        )

    def get(self, path: str) -> Dict[str, Any]:
        if self.card_path is not None and path != self.card_path:
            raise _NotFound(path)
        return agent_card(self.base_url)


def _client(card_path: str | None = None):
    cp = ControlPlane.in_memory()
    service = A2AService(cp)
    transport = _InProcessTransport(service, base_url="https://remote.test", card_path=card_path)
    client = A2AClient("https://remote.test", transport=transport)
    return client, cp, transport


# -- delegation round-trip ---------------------------------------------------


def test_send_message_creates_real_ledger_task():
    client, cp, transport = _client()

    task = client.send_message("do X\nwith details")

    # The returned Task's id is a *real* task in the remote control plane.
    assert task.status.state == TaskState.SUBMITTED
    assert task.context_id == "a2a"
    ledger_task = cp.get_task(task.id)
    assert ledger_task.state == MacTaskState.OPEN.value
    assert ledger_task.title == "do X"
    assert ledger_task.description == "do X\nwith details"

    # The client built a real message/send JSON-RPC request with a text part.
    sent = transport.posted[-1]
    assert sent["jsonrpc"] == "2.0"
    assert sent["method"] == "message/send"
    part = sent["params"]["message"]["parts"][0]
    assert part == {"kind": "text", "text": "do X\nwith details"}
    # A messageId was generated for us.
    assert sent["params"]["message"]["messageId"]


def test_send_message_honours_explicit_ids():
    client, _cp, transport = _client()

    client.send_message("hi", context_id="ctx-7", message_id="msg-fixed")

    sent = transport.posted[-1]["params"]
    assert sent["contextId"] == "ctx-7"
    assert sent["message"]["messageId"] == "msg-fixed"
    assert sent["message"]["contextId"] == "ctx-7"


def test_get_task_reflects_state():
    client, cp, _transport = _client()
    task = client.send_message("work item")

    assert client.get_task(task.id).status.state == TaskState.SUBMITTED

    # Drive the remote ledger into a "working" state and confirm the projection.
    cp.transition_task(task.id, MacTaskState.CLAIMED.value, actor="tester")
    assert client.get_task(task.id).status.state == TaskState.WORKING


def test_cancel_task_cancels_remote_ledger_task():
    client, cp, _transport = _client()
    task = client.send_message("cancel me")

    cancelled = client.cancel_task(task.id)

    assert cancelled.status.state == TaskState.CANCELED
    assert cp.get_task(task.id).state == MacTaskState.CANCELLED.value


# -- JSON-RPC errors surface as A2AClientError -------------------------------


def test_get_unknown_task_raises_task_not_found():
    client, _cp, _transport = _client()

    with pytest.raises(A2AClientError) as excinfo:
        client.get_task("task_missing")
    assert excinfo.value.code == -32001
    assert "task_missing" in excinfo.value.message


def test_send_empty_message_raises_invalid_params():
    # An empty/whitespace message has no text part -> service returns -32602.
    client, _cp, _transport = _client()

    with pytest.raises(A2AClientError) as excinfo:
        client.send_message("   ")
    assert excinfo.value.code == -32602


# -- agent card discovery + legacy fallback ----------------------------------


def test_fetch_agent_card_canonical_path():
    # Default fake transport serves the card on every well-known path.
    client, _cp, _transport = _client()

    card = client.fetch_agent_card()
    assert card["name"] == "mac"
    assert card["url"] == "https://remote.test/a2a"


def test_fetch_agent_card_falls_back_to_legacy_on_404():
    # Agent only serves the pre-v0.3 /.well-known/agent.json alias.
    client, _cp, _transport = _client(card_path="/.well-known/agent.json")

    card = client.fetch_agent_card()
    assert card["name"] == "mac"


# -- default urllib transport request building (stdlib-mock) -----------------


def test_urllib_transport_posts_jsonrpc_with_bearer(monkeypatch):
    captured: Dict[str, Any] = {}

    class _FakeResp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def _fake_urlopen(req, *args, **kwargs):  # noqa: ANN001 - urllib.Request
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["data"] = req.data
        # urllib normalizes header keys to Capitalized form.
        captured["headers"] = dict(req.header_items())
        return _FakeResp(b'{"jsonrpc":"2.0","id":"a2a-out-1","result":{"ok":true}}')

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    transport = _UrllibTransport("https://remote.test/", token="sekret")
    body = {"jsonrpc": "2.0", "id": "a2a-out-1", "method": "tasks/get", "params": {"id": "t1"}}
    out = transport.post("/a2a", body)

    assert out == {"jsonrpc": "2.0", "id": "a2a-out-1", "result": {"ok": True}}
    assert captured["url"] == "https://remote.test/a2a"
    assert captured["method"] == "POST"
    # Bearer header is present (urllib title-cases header keys).
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers["authorization"] == "Bearer sekret"
    assert headers["content-type"] == "application/json"
    # The POST body is the JSON-RPC request we handed in.
    assert json.loads(captured["data"].decode("utf-8")) == body


def test_urllib_transport_get_raises_notfound_on_404(monkeypatch):
    import urllib.error

    def _fake_urlopen(req, *args, **kwargs):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    transport = _UrllibTransport("https://remote.test")
    with pytest.raises(_NotFound):
        transport.get("/.well-known/agent-card.json")
