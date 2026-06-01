"""th-merge-02: the in-mac OpenAI front door (ProviderProxy + gated mount)."""

from __future__ import annotations

from mac.provider_router import Provider, ProviderRouter
from mac.router_app import ProviderProxy, build_proxy_from_env, mount_router, resolve_provider_key


def _router():
    return ProviderRouter(
        [Provider("primary", "http://p/v1", priority=0), Provider("secondary", "http://s/v1", priority=1)],
        failure_threshold=1,
        cooldown_seconds=1000.0,
    )


def _fake_forward(responses):
    """responses: dict provider_name -> (status, body) or a callable(payload)."""
    calls = []

    def fwd(provider, path, payload, *, timeout=60.0):
        calls.append((provider.name, path))
        r = responses[provider.name]
        return r(payload) if callable(r) else r

    return fwd, calls


def test_routes_to_primary_on_success_and_records():
    r = _router()
    fwd, calls = _fake_forward({"primary": (200, {"ok": True})})
    proxy = ProviderProxy(r, fwd)
    status, body = proxy.complete("/chat/completions", {"model": "*"})
    assert status == 200 and body == {"ok": True}
    assert calls == [("primary", "/chat/completions")]
    assert r.status()["primary"]["state"] == "closed"


def test_fails_over_to_secondary_on_5xx():
    r = _router()
    fwd, calls = _fake_forward({"primary": (503, {"error": "down"}), "secondary": (200, {"ok": True})})
    proxy = ProviderProxy(r, fwd)
    status, body = proxy.complete("/chat/completions", {"model": "*"})
    assert status == 200 and body == {"ok": True}
    assert [c[0] for c in calls] == ["primary", "secondary"]   # tried primary, failed over
    assert r.status()["primary"]["state"] == "open"            # breaker tripped on primary


def test_429_is_treated_as_failure_and_fails_over():
    r = _router()
    fwd, _ = _fake_forward({"primary": (429, {"error": "rate"}), "secondary": (200, {"ok": 1})})
    status, _ = ProviderProxy(r, fwd).complete("/chat/completions", {"model": "*"})
    assert status == 200
    assert r.status()["primary"]["state"] == "open"


def test_network_error_status_none_fails_over():
    r = _router()
    fwd, _ = _fake_forward({"primary": (None, {"error": "timeout"}), "secondary": (200, {"ok": 1})})
    status, _ = ProviderProxy(r, fwd).complete("/chat/completions", {"model": "*"})
    assert status == 200
    assert r.status()["primary"]["state"] == "open"


def test_4xx_is_returned_not_failed_over():
    # A 400 is a bad request, not a provider health problem: return it, keep the
    # provider healthy, and do NOT pointlessly retry the same bad request elsewhere.
    r = _router()
    fwd, calls = _fake_forward({"primary": (400, {"error": "bad request"})})
    status, body = ProviderProxy(r, fwd).complete("/chat/completions", {"model": "*"})
    assert status == 400 and body == {"error": "bad request"}
    assert [c[0] for c in calls] == ["primary"]                # no failover
    assert r.status()["primary"]["state"] == "closed"          # provider stays healthy


def test_all_providers_failing_returns_503_failfast():
    r = _router()
    fwd, _ = _fake_forward({"primary": (500, {}), "secondary": (500, {})})
    status, body = ProviderProxy(r, fwd).complete("/chat/completions", {"model": "*"})
    assert status == 503
    assert body["error"]["type"] == "all_providers_unavailable"
    assert {a["provider"] for a in body["error"]["attempts"]} == {"primary", "secondary"}


def test_mount_is_noop_unless_inproc(monkeypatch):
    from fastapi import FastAPI

    app = FastAPI()
    # default backend (tokenhub) -> not mounted
    assert mount_router(app, env={}) is False
    # inproc but no providers -> still not mounted (nothing to route to)
    assert mount_router(app, env={"MAC_ROUTER_BACKEND": "inproc"}) is False


def test_mount_adds_routes_when_inproc_with_providers():
    from fastapi import FastAPI

    app = FastAPI()
    proxy = ProviderProxy(_router(), _fake_forward({"primary": (200, {"ok": True})})[0])
    assert mount_router(app, env={"MAC_ROUTER_BACKEND": "inproc"}, proxy=proxy) is True
    paths = {r.path for r in app.routes}
    assert "/v1/chat/completions" in paths and "/v1/embeddings" in paths


def test_build_proxy_from_env_reads_providers():
    env = {"MAC_ROUTER_PROVIDERS": "p=http://p/v1,0,key=P_KEY;s=http://s/v1,1", "MAC_ROUTER_BACKEND": "inproc"}
    proxy = build_proxy_from_env(env)
    assert proxy is not None
    assert build_proxy_from_env({}) is None  # no providers configured


# -- wildcard -> concrete model resolution (th-merge-02b) --------------------


def _capturing_forward(status, body):
    """Forward that records the exact payload it was handed."""
    seen = []

    def fwd(provider, path, payload, *, timeout=60.0):
        seen.append(payload)
        return status, body

    return fwd, seen


def test_wildcard_model_resolves_to_default_before_forward():
    # The real upstream rejects model="*"; the proxy must substitute the
    # configured concrete model before forwarding.
    r = _router()
    fwd, seen = _capturing_forward(200, {"ok": True})
    proxy = ProviderProxy(r, fwd, default_model="meta/llama-3.3-70b-instruct")
    status, _ = proxy.complete("/chat/completions", {"model": "*", "messages": []})
    assert status == 200
    assert seen[0]["model"] == "meta/llama-3.3-70b-instruct"


def test_empty_model_resolves_and_concrete_model_passes_through():
    r = _router()
    fwd, seen = _capturing_forward(200, {"ok": True})
    proxy = ProviderProxy(r, fwd, default_model="meta/llama-3.3-70b-instruct")
    proxy.complete("/chat/completions", {})                       # missing model -> default
    proxy.complete("/chat/completions", {"model": "azure/openai/o4-mini"})  # concrete -> untouched
    assert seen[0]["model"] == "meta/llama-3.3-70b-instruct"
    assert seen[1]["model"] == "azure/openai/o4-mini"


def test_no_default_model_leaves_wildcard_untouched():
    r = _router()
    fwd, seen = _capturing_forward(200, {"ok": True})
    ProviderProxy(r, fwd).complete("/chat/completions", {"model": "*"})
    assert seen[0]["model"] == "*"   # no default configured -> unchanged (back-compat)


# -- streaming passthrough (th-merge-02b) ------------------------------------


def _fake_stream(responses):
    """responses: dict provider_name -> (status, iterator|body)."""
    calls = []

    def fwd(provider, path, payload, *, timeout=300.0):
        calls.append((provider.name, payload.get("model"), payload.get("stream")))
        return responses[provider.name]

    return fwd, calls


def test_stream_returns_iterator_on_2xx_and_records_success():
    r = _router()
    chunks = iter([b"data: {\"x\":1}\n\n", b"data: [DONE]\n\n"])
    sfwd, calls = _fake_stream({"primary": (200, chunks)})
    proxy = ProviderProxy(r, _fake_forward({})[0], stream_forward_fn=sfwd,
                          default_model="meta/llama-3.3-70b-instruct")
    status, obj = proxy.stream_complete("/chat/completions", {"model": "*"})
    assert status == 200
    assert b"".join(obj) == b"data: {\"x\":1}\n\ndata: [DONE]\n\n"
    assert calls[0][1] == "meta/llama-3.3-70b-instruct"   # wildcard resolved
    assert calls[0][2] is True                            # stream forced on
    assert r.status()["primary"]["state"] == "closed"


def test_stream_fails_over_to_secondary_on_5xx_before_bytes():
    r = _router()
    good = iter([b"data: ok\n\n"])
    sfwd, calls = _fake_stream({"primary": (503, {"error": "down"}), "secondary": (200, good)})
    proxy = ProviderProxy(r, _fake_forward({})[0], stream_forward_fn=sfwd)
    status, obj = proxy.stream_complete("/chat/completions", {"model": "*"})
    assert status == 200 and b"".join(obj) == b"data: ok\n\n"
    assert [c[0] for c in calls] == ["primary", "secondary"]
    assert r.status()["primary"]["state"] == "open"


def test_stream_failfast_when_all_providers_down():
    r = _router()
    sfwd, _ = _fake_stream({"primary": (500, {}), "secondary": (None, {"error": "timeout"})})
    proxy = ProviderProxy(r, _fake_forward({})[0], stream_forward_fn=sfwd)
    status, body = proxy.stream_complete("/chat/completions", {"model": "*"})
    assert status == 503 and body["error"]["type"] == "all_providers_unavailable"


def test_stream_without_transport_degrades_to_buffered_completion():
    r = _router()
    fwd, calls = _fake_forward({"primary": (200, {"ok": True})})
    proxy = ProviderProxy(r, fwd)   # no stream_forward_fn
    status, body = proxy.stream_complete("/chat/completions", {"model": "*", "stream": True})
    assert status == 200 and body == {"ok": True}
    assert calls == [("primary", "/chat/completions")]


# -- th-merge-04: provider key from the encrypted secret store ---------------


def test_resolve_provider_key_from_env(monkeypatch):
    monkeypatch.setenv("MY_UPSTREAM_KEY", "env-secret-123")
    p = Provider("nvidia", "http://u/v1", api_key_env="MY_UPSTREAM_KEY")
    assert resolve_provider_key(p) == "env-secret-123"


def test_resolve_provider_key_from_secret_store():
    # key=secret:<name> is resolved via the injected escrow resolver, NOT env.
    p = Provider("nvidia", "http://u/v1", api_key_env="secret:nvidia-upstream")
    resolver = {"nvidia-upstream": "escrowed-nvapi-key"}.get
    assert resolve_provider_key(p, resolver) == "escrowed-nvapi-key"


def test_resolve_provider_key_secret_missing_resolver_or_value():
    p = Provider("nvidia", "http://u/v1", api_key_env="secret:absent")
    assert resolve_provider_key(p, None) == ""              # no resolver wired
    assert resolve_provider_key(p, {}.get) == ""            # resolver returns None
    p2 = Provider("nvidia", "http://u/v1", api_key_env="")
    assert resolve_provider_key(p2) == ""                   # no key spec


def test_urllib_forwarder_uses_secret_resolver(monkeypatch):
    # The real forwarder builds an Authorization header from the resolved key.
    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=60.0):
        captured["auth"] = req.headers.get("Authorization")
        return _Resp()

    from mac import router_app

    monkeypatch.setattr(router_app.urllib.request, "urlopen", fake_urlopen)
    p = Provider("nvidia", "http://u/v1", api_key_env="secret:nvidia-upstream")
    status, body = router_app.urllib_forwarder(
        p, "/chat/completions", {"model": "x"}, secret_resolver={"nvidia-upstream": "escrowed-key"}.get
    )
    assert status == 200 and body == {"ok": True}
    assert captured["auth"] == "Bearer escrowed-key"


def test_mount_streams_through_fastapi():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    sse = [b"data: {\"delta\":\"hi\"}\n\n", b"data: [DONE]\n\n"]
    sfwd, _ = _fake_stream({"primary": (200, iter(sse))})
    proxy = ProviderProxy(_router(), _fake_forward({"primary": (200, {})})[0], stream_forward_fn=sfwd)
    app = FastAPI()
    assert mount_router(app, env={"MAC_ROUTER_BACKEND": "inproc"}, proxy=proxy) is True
    client = TestClient(app)
    resp = client.post("/v1/chat/completions", json={"model": "*", "stream": True})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.content == b"".join(sse)
