"""The MAC HTTP client must wrap ALL transport failures in MacApiError.

Regression guard (2026-07-14): a read-timeout raises a bare ``TimeoutError``
that ``http.client`` does NOT wrap in ``urllib.error.URLError``. Left unwrapped
it escaped every caller's ``except MacApiError`` and killed a worker's
lease-renewal thread on a hub blip, cascading into lease loss and a wedged claim
loop. Every socket-level transient (timeout / connection reset / broken pipe)
must surface as MacApiError so callers recover uniformly.
"""

from __future__ import annotations

import urllib.error

import pytest

import mac.api_client as api_client
from mac.api_client import MacApiClient, MacApiError


def _client():
    return MacApiClient(base_url="http://hub.invalid:8789", token="t", timeout=1.0)


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),  # read timeout (the observed bug)
        ConnectionResetError("connection reset"),  # peer reset
        BrokenPipeError("broken pipe"),  # write failure
        OSError("generic socket error"),  # any other socket OSError
    ],
)
def test_transient_socket_errors_become_mac_api_error(monkeypatch, exc):
    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(api_client.urllib.request, "urlopen", boom)
    with pytest.raises(MacApiError) as ei:
        _client().get("/tasks/task_x")
    # the original transient error is chained for diagnosis
    assert ei.value.__cause__ is exc
    assert "transient transport error" in str(ei.value)


def test_urlerror_and_httperror_still_wrapped(monkeypatch):
    # URLError path (unchanged behavior)
    monkeypatch.setattr(
        api_client.urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("no route")),
    )
    with pytest.raises(MacApiError):
        _client().get("/tasks/task_x")

    # HTTPError path (unchanged behavior)
    class _He(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("http://x", 500, "boom", {}, None)

        def read(self):  # noqa: D401 - minimal stub
            return b"server error"

    monkeypatch.setattr(
        api_client.urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(_He()),
    )
    with pytest.raises(MacApiError):
        _client().post("/leases/l/renew", {"agent_id": "a"})
