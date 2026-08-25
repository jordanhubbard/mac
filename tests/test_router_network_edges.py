"""Network-boundary coverage for the in-process provider router."""

from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

from mac.provider_router import Provider
from mac import router_app


class _Response:
    def __init__(self, body=b"", *, status=200, content_type="application/json", chunks=None):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.chunks = iter(chunks) if chunks is not None else None
        self.closed = False

    def read(self, _size=-1):
        if self.chunks is not None:
            return next(self.chunks, b"")
        return self.body

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _provider():
    return Provider("provider", "https://provider.test/v1", api_key_env="API_KEY")


def _http_error(body=b"", *, code=400, reason="bad"):
    return urllib.error.HTTPError("https://provider.test", code, reason, {}, io.BytesIO(body))


def test_payload_token_floor_and_context_edges(monkeypatch) -> None:
    payload = {"model": "x"}
    assert router_app._normalize_payload(payload, ()) is payload
    assert router_app._normalize_payload(payload, ("missing",)) is payload
    monkeypatch.setenv("MAC_ROUTER_MAX_TOKENS_FLOOR", "bad")
    assert (
        router_app._ensure_max_tokens_floor({"max_tokens": "bad"}, "/chat/completions")[
            "max_tokens"
        ]
        == 32000
    )
    assert router_app._default_model_from_env({"MAC_ROUTER_DEFAULT_MODEL": "*"})
    body = {
        "_mac_context": {"agent_id": "first", "task_id": "task"},
        "metadata": {"mac_context": {"agent_id": "second", "fleet": "fleet"}},
    }
    assert router_app._body_route_context(body) == {
        "agent_id": "first",
        "task_id": "task",
        "fleet": "fleet",
    }


def test_multipart_encoder_accepts_fields_and_file() -> None:
    content_type, body = router_app._encode_multipart(
        {
            "fields": {"model": "whisper"},
            "file": {
                "name": "audio",
                "filename": "sample.wav",
                "content_type": "audio/wav",
                "b64": "aGVsbG8=",
            },
        }
    )
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b"sample.wav" in body and b"hello" in body


def test_urllib_forwarder_json_binary_and_multipart(monkeypatch) -> None:
    responses = iter(
        [
            _Response(json.dumps({"ok": True}).encode()),
            _Response(b"", status=204),
            _Response(b"binary", content_type="audio/wav; charset=binary"),
            _Response(b'{"job":"done"}', content_type="application/json"),
            _Response(b'{"uploaded":true}'),
        ]
    )
    seen = []
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setattr(
        router_app.urllib.request, "urlopen", lambda req, **_k: seen.append(req) or next(responses)
    )
    assert router_app.urllib_forwarder(_provider(), "/chat", {"x": 1}) == (200, {"ok": True})
    assert router_app.urllib_forwarder(_provider(), "/empty", {}) == (204, {})
    status, binary = router_app.urllib_forwarder(_provider(), "/audio", {}, binary_ok=True)
    assert status == 200 and binary["bytes"] == 6 and binary["content_type"] == "audio/wav"
    assert router_app.urllib_forwarder(_provider(), "/json", {}, binary_ok=True)[1] == {
        "job": "done"
    }
    status, body = router_app.urllib_forwarder(
        _provider(), "/upload", {"__multipart__": {"fields": {"x": "1"}}}
    )
    assert status == 200 and body == {"uploaded": True}
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert seen[-1].headers["Content-type"].startswith("multipart/form-data")


def test_urllib_forwarder_error_shapes(monkeypatch) -> None:
    errors = iter(
        [
            _http_error(b'{"error":{"message":"json"}}', code=401),
            _http_error(b"plain failure", code=502),
            _http_error(b"", code=503, reason="unavailable"),
            RuntimeError("offline"),
        ]
    )
    monkeypatch.setattr(
        router_app.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(next(errors))
    )
    assert router_app.urllib_forwarder(_provider(), "/x", {})[0] == 401
    assert (
        router_app.urllib_forwarder(_provider(), "/x", {})[1]["error"]["message"] == "plain failure"
    )
    assert (
        router_app.urllib_forwarder(_provider(), "/x", {})[1]["error"]["message"] == "unavailable"
    )
    status, body = router_app.urllib_forwarder(_provider(), "/x", {})
    assert status is None and body["error"]["type"] == "upstream_unreachable"


def test_drain_chunks_closes_on_success_and_close_failure() -> None:
    response = _Response(chunks=[b"one", b"two", b""])
    assert list(router_app._drain_chunks(response, 3)) == [b"one", b"two"]
    assert response.closed is True
    response = _Response(chunks=[b""])
    response.close = lambda: (_ for _ in ()).throw(OSError("gone"))
    assert list(router_app._drain_chunks(response)) == []


def test_stream_forwarder_success_http_errors_and_transport(monkeypatch) -> None:
    response = _Response(status=201, chunks=[b"data", b""])
    values = iter(
        [
            response,
            _http_error(b'{"error":{"message":"bad"}}', code=400),
            _http_error(b"plain", code=502),
            RuntimeError("offline"),
        ]
    )

    def urlopen(*_args, **_kwargs):
        value = next(values)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(router_app.urllib.request, "urlopen", urlopen)
    status, chunks = router_app.urllib_stream_forwarder(_provider(), "/stream", {})
    assert status == 201 and list(chunks) == [b"data"]
    assert router_app.urllib_stream_forwarder(_provider(), "/stream", {})[0] == 400
    assert (
        router_app.urllib_stream_forwarder(_provider(), "/stream", {})[1]["error"]["message"]
        == "plain"
    )
    status, body = router_app.urllib_stream_forwarder(_provider(), "/stream", {})
    assert status is None and body["error"]["type"] == "upstream_unreachable"


def test_get_json_success_errors_and_transport(monkeypatch) -> None:
    values = iter(
        [
            _Response(b'{"ok":true}'),
            _Response(b"", status=204),
            _http_error(b'{"error":"bad"}', code=404),
            _http_error(b"plain", code=500),
            RuntimeError("offline"),
        ]
    )

    def urlopen(*_args, **_kwargs):
        value = next(values)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(router_app.urllib.request, "urlopen", urlopen)
    assert router_app.urllib_get_json(_provider(), "/job") == (200, {"ok": True})
    assert router_app.urllib_get_json(_provider(), "/job") == (204, {})
    assert router_app.urllib_get_json(_provider(), "/job")[0] == 404
    assert router_app.urllib_get_json(_provider(), "/job")[1]["error"]["message"] == "plain"
    assert router_app.urllib_get_json(_provider(), "/job")[0] is None


def test_build_proxy_invalid_numeric_env_and_forward_closures(monkeypatch) -> None:
    proxy = router_app.build_proxy_from_env(
        {
            "MAC_ROUTER_PROVIDERS": "primary=https://provider.test/v1,0",
            "MAC_ROUTER_FAILURE_THRESHOLD": "bad",
            "MAC_ROUTER_COOLDOWN_SECONDS": "bad",
            "MAC_ROUTER_TIMEOUT": "bad",
            "MAC_ROUTER_STREAM_TIMEOUT": "bad",
        }
    )
    assert proxy is not None
    monkeypatch.setattr(router_app, "urllib_forwarder", lambda *_a, **_k: (200, {"ok": True}))
    assert proxy._forward(proxy._router._order[0], "/x", {}) == (200, {"ok": True})


def test_proxy_outcome_status_matrix() -> None:
    assert router_app.ProviderProxy._outcome_for_status(204) == "success"
    assert router_app.ProviderProxy._outcome_for_status(429) == "provider_error"
    assert router_app.ProviderProxy._outcome_for_status(503) == "provider_error"
    assert router_app.ProviderProxy._outcome_for_status(400) == "client_error"
    assert router_app.ProviderProxy._outcome_for_status(302) == "answered"
