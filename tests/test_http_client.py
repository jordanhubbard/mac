"""Public HubClient transport contract: auth header, JSON body, documented errors."""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from mac.http_client import HubClient, HubClientError


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload


def test_request_sends_bearer_token_and_json_body(monkeypatch) -> None:
    seen = []

    def open_ok(request, timeout):
        seen.append((request, timeout))
        return _Context(_Response(b'{"ok": true}'))

    monkeypatch.setattr("mac.http_client.urllib.request.urlopen", open_ok)
    client = HubClient("https://hub.example/", token="token")
    assert client.request("POST", "/tasks", {"title": "test"}) == {"ok": True}
    request, timeout = seen[-1]
    assert timeout == 30
    assert request.full_url == "https://hub.example/tasks"
    assert request.headers["Authorization"] == "Bearer token"
    assert json.loads(request.data) == {"title": "test"}


def test_empty_body_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(
        "mac.http_client.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Context(_Response(b"")),
    )
    assert HubClient("https://hub.example").request("GET", "/health") is None


def test_http_and_transport_errors_become_hub_client_error(monkeypatch) -> None:
    client = HubClient("https://hub.example/", token="token")

    def http_error(*_args, **_kwargs):
        raise HTTPError(
            "https://hub.example/tasks",
            403,
            "Forbidden",
            {},
            io.BytesIO(b"scope denied"),
        )

    monkeypatch.setattr("mac.http_client.urllib.request.urlopen", http_error)
    with pytest.raises(HubClientError, match="scope denied"):
        client.request("GET", "/tasks")

    def url_error(*_args, **_kwargs):
        raise URLError("offline")

    monkeypatch.setattr("mac.http_client.urllib.request.urlopen", url_error)
    with pytest.raises(HubClientError, match="offline"):
        client.request("GET", "/tasks")
