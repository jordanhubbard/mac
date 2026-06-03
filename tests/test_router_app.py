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


def test_transient_401_is_retried_once_on_same_provider():
    """A transient upstream 401 (e.g. NVIDIA gateway momentarily can't reach its
    key-verification DB) must not kill a long agent session. The router retries
    the SAME provider once; if the retry succeeds, the client never sees the
    blip."""
    r = _router()
    seq = iter([(401, {"error": {"message": "Can't reach database server", "code": "401"}}),
                (200, {"ok": True})])

    def fwd(provider, path, payload, *, timeout=60.0):
        return next(seq)

    status, body = ProviderProxy(r, fwd).complete("/chat/completions", {"model": "primary-model"})
    assert status == 200 and body == {"ok": True}


def test_persistent_401_returned_after_bounded_retry():
    """A genuine bad-key 401 keeps returning 401 on retry. The router must NOT
    loop forever: it retries once, still gets 401, returns it to the client."""
    r = _router()
    calls = []

    def fwd(provider, path, payload, *, timeout=60.0):
        calls.append(provider.name)
        return 401, {"error": {"message": "invalid api key"}}

    status, body = ProviderProxy(r, fwd).complete("/chat/completions", {"model": "primary-model"})
    assert status == 401
    # bounded: the same provider is tried at most twice (initial + one retry),
    # never an unbounded loop.
    assert calls.count("primary") <= 2


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


def test_strips_unsupported_reasoning_summary_param_before_forwarding():
    """A client (opencode) may send `reasoningSummary` for an OpenAI reasoning
    model; some upstreams reject it with 400 unknown_parameter. The router must
    strip known-unsupported params before forwarding so the request succeeds,
    while leaving normal params untouched."""
    r = _router()
    seen = {}

    def fwd(provider, path, payload, *, timeout=60.0):
        seen.update(payload)
        return 200, {"ok": True}

    proxy = ProviderProxy(r, fwd)
    status, _ = proxy.complete(
        "/chat/completions",
        {"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}], "reasoningSummary": "auto"},
    )
    assert status == 200
    assert "reasoningSummary" not in seen  # stripped
    assert seen["model"] == "gpt-5.5"  # normal params preserved
    assert seen["messages"] == [{"role": "user", "content": "hi"}]


def test_build_proxy_drop_params_overridable_via_env():
    """Operators can extend the stripped-param set declaratively via
    MAC_ROUTER_DROP_PARAMS (comma-separated) without a code change."""
    proxy = build_proxy_from_env(
        env={
            "MAC_ROUTER_PROVIDERS": "primary=http://p/v1,0",
            "MAC_ROUTER_DROP_PARAMS": "reasoningSummary,foo_param",
        }
    )
    assert proxy is not None
    assert "reasoningSummary" in proxy._drop_params
    assert "foo_param" in proxy._drop_params


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


def test_route_logging_is_configured_to_emit_info():
    # Regression: uvicorn --log-level info only configures uvicorn's own loggers,
    # so mac.router INFO propagated to a handler-less root and was dropped. The
    # router must attach its own INFO handler so `router: route …` reaches the journal.
    import logging

    from mac.router_app import _configure_route_logging
    from mac.router_app import logger as router_logger

    saved = (
        router_logger.level,
        router_logger.propagate,
        list(router_logger.handlers),
        getattr(router_logger, "_mac_route_logging_configured", False),
    )
    try:
        router_logger.handlers.clear()
        router_logger.propagate = True
        router_logger.setLevel(logging.NOTSET)
        if hasattr(router_logger, "_mac_route_logging_configured"):
            del router_logger._mac_route_logging_configured

        _configure_route_logging()
        assert router_logger.isEnabledFor(logging.INFO)
        assert router_logger.hasHandlers()

        # idempotent: a second mount must not stack handlers
        n = len(router_logger.handlers)
        _configure_route_logging()
        assert len(router_logger.handlers) == n
    finally:
        router_logger.setLevel(saved[0])
        router_logger.propagate = saved[1]
        router_logger.handlers[:] = saved[2]
        if saved[3]:
            router_logger._mac_route_logging_configured = True
        elif hasattr(router_logger, "_mac_route_logging_configured"):
            del router_logger._mac_route_logging_configured


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


def test_image_proxy_forwards_to_upstream_with_vault_key(monkeypatch):
    # Stream B "hub serves them": a spoke POSTs image-gen to the hub's /v1/genai
    # with its hub token; the hub swaps in the vault image key and forwards to NIM.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mac import router_app

    captured = {}

    class _Resp:
        status = 200

        def read(self):
            return b'{"artifacts":[{"base64":"IMG"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=60.0):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return _Resp()

    monkeypatch.setattr(router_app.urllib.request, "urlopen", fake_urlopen)

    app = FastAPI()
    env = {
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_IMAGE_UPSTREAM": "https://ai.api.nvidia.com/v1/genai",
        "MAC_ROUTER_IMAGE_KEY": "secret:nvidia-image",
    }
    # image proxy mounts even with NO chat providers configured
    assert mount_router(app, env=env, secret_resolver={"nvidia-image": "escrowed-image-key"}.get) is True

    client = TestClient(app)
    r = client.post("/v1/genai/black-forest-labs/flux.1-dev", json={"prompt": "a cat"})
    assert r.status_code == 200
    assert r.json() == {"artifacts": [{"base64": "IMG"}]}
    # forwarded to <upstream>/<path>, with the spoke's token swapped for the vault key
    assert captured["url"] == "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev"
    assert captured["auth"] == "Bearer escrowed-image-key"


def test_image_proxy_noop_without_config():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    # inproc but no MAC_ROUTER_IMAGE_* and no chat providers -> nothing mounted
    assert mount_router(app, env={"MAC_ROUTER_BACKEND": "inproc"}) is False
    r = TestClient(app).post("/v1/genai/x/y", json={})
    assert r.status_code == 404


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


# -- th-merge-06: wildcard model ladder (on-the-fly substitution) -------------


def _model_forward(by_model, default=(200, {"ok": True})):
    """Forward keyed by the outgoing payload's model, recording the sequence."""
    calls = []

    def fwd(provider, path, payload, *, timeout=60.0):
        m = payload.get("model")
        calls.append(m)
        return by_model.get(m, default)

    return fwd, calls


def test_wildcard_ladder_substitutes_on_model_unavailable():
    r = _router()
    fwd, calls = _model_forward({"m1": (404, {"error": "no model"}), "m2": (200, {"ok": 1})})
    proxy = ProviderProxy(r, fwd, wildcard_models=("m1", "m2", "m3"))
    status, body = proxy.complete("/chat/completions", {"model": "*"})
    assert status == 200 and body == {"ok": 1}
    assert calls[:2] == ["m1", "m2"]                      # rank-0 404 -> substitute rank-1
    assert r.status()["primary"]["state"] == "closed"     # 404 is not a provider failure


def test_wildcard_ladder_exhausted_returns_last_model_response():
    r = _router()
    fwd, calls = _model_forward({"m1": (404, {"e": 1}), "m2": (422, {"e": 2})})
    proxy = ProviderProxy(r, fwd, wildcard_models=("m1", "m2"))
    status, body = proxy.complete("/chat/completions", {"model": "*"})
    assert status == 422 and body == {"e": 2}             # last candidate returned as-is
    assert calls == ["m1", "m2"]


def test_concrete_model_is_not_laddered():
    r = _router()
    fwd, calls = _model_forward({"gpt-x": (404, {"e": 1})})
    proxy = ProviderProxy(r, fwd, wildcard_models=("m1", "m2"))
    status, _ = proxy.complete("/chat/completions", {"model": "gpt-x"})
    assert status == 404 and calls == ["gpt-x"]           # concrete request: ladder not consulted


def test_400_is_not_a_model_retry_code():
    # A 400 is a genuine bad request, not "this model is unavailable" -> return it,
    # do not walk the rest of the ladder.
    r = _router()
    fwd, calls = _model_forward({"m1": (400, {"e": "bad"})})
    proxy = ProviderProxy(r, fwd, wildcard_models=("m1", "m2"))
    status, _ = proxy.complete("/chat/completions", {"model": "*"})
    assert status == 400 and calls == ["m1"]


def test_wildcard_ladder_failfast_when_providers_down_does_not_walk_models():
    r = _router()  # failure_threshold=1
    fwd, calls = _model_forward({}, default=(503, {"down": 1}))   # every model -> provider failure
    proxy = ProviderProxy(r, fwd, wildcard_models=("m1", "m2", "m3"))
    status, body = proxy.complete("/chat/completions", {"model": "*"})
    assert status == 503 and body["error"]["type"] == "all_providers_unavailable"
    assert "m2" not in calls and "m3" not in calls        # dead providers -> don't walk the ladder


def test_build_proxy_reads_wildcard_models():
    env = {
        "MAC_ROUTER_PROVIDERS": "p=http://p/v1,0",
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_WILDCARD_MODELS": "meta/llama-3.3-70b-instruct|meta/llama-3.1-8b-instruct",
    }
    proxy = build_proxy_from_env(env)
    assert proxy is not None
    assert proxy._wildcard_models == ("meta/llama-3.3-70b-instruct", "meta/llama-3.1-8b-instruct")
