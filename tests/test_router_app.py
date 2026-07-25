"""th-merge-02: the in-mac OpenAI front door (ProviderProxy + gated mount)."""

from __future__ import annotations

import mac.router_app as _ra
from mac.provider_router import Provider, ProviderRouter
from mac.router_app import (
    ProviderProxy,
    _ensure_max_tokens_floor,
    build_proxy_from_env,
    mac_route_context_headers,
    mount_router,
    resolve_provider_key,
)


def test_max_tokens_floor_rescues_large_chat_generations(monkeypatch):
    # mac: agents (e.g. the taskbrain CLI task) truncated at a low default
    # max_tokens; the router raises chat completions to a floor so large
    # single-turn file generations don't get capped (finish_reason=length).
    monkeypatch.delenv("MAC_ROUTER_MAX_TOKENS_FLOOR", raising=False)
    assert _ensure_max_tokens_floor({"model": "x"}, "/chat/completions")["max_tokens"] == 32000
    assert _ensure_max_tokens_floor({"max_tokens": 4096}, "/chat/completions")["max_tokens"] == 32000
    # an already-generous request is left alone
    assert _ensure_max_tokens_floor({"max_tokens": 50000}, "/chat/completions")["max_tokens"] == 50000
    # embeddings are never touched
    assert "max_tokens" not in _ensure_max_tokens_floor({"model": "e"}, "/embeddings")
    # configurable + disable
    monkeypatch.setenv("MAC_ROUTER_MAX_TOKENS_FLOOR", "8000")
    assert _ensure_max_tokens_floor({"max_tokens": 100}, "/chat/completions")["max_tokens"] == 8000
    monkeypatch.setenv("MAC_ROUTER_MAX_TOKENS_FLOOR", "0")
    assert "max_tokens" not in _ensure_max_tokens_floor({"model": "x"}, "/chat/completions")


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


def test_records_secret_free_llm_route_observation_with_context():
    observed = []
    r = _router()
    fwd, _ = _fake_forward(
        {
            "primary": (
                200,
                {
                    "model": "served-model",
                    "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
                },
            )
        }
    )
    proxy = ProviderProxy(r, fwd, default_model="resolved-model", route_observer=observed.append)

    status, _ = proxy.complete(
        "/chat/completions",
        {"model": "*", "messages": [{"role": "user", "content": "secret prompt"}]},
        route_context={"agent_id": "agent_1", "task_id": "task_1", "lease_id": "lease_1"},
    )

    assert status == 200
    assert len(observed) == 1
    event = observed[0]
    assert event["schema"] == "mac.llm_route.v1"
    assert event["provider"] == "primary"
    assert event["requested_model"] == "*"
    assert event["resolved_model"] == "resolved-model"
    assert event["response_model"] == "served-model"
    assert event["status_code"] == 200
    assert event["agent_id"] == "agent_1"
    assert event["task_id"] == "task_1"
    assert event["lease_id"] == "lease_1"
    assert event["usage"]["total_tokens"] == 9
    assert "duration_ms" in event
    assert "secret prompt" not in str(event)


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


def test_strips_internal_mac_context_before_forwarding():
    r = _router()
    seen = {}

    def fwd(provider, path, payload, *, timeout=60.0):
        seen.update(payload)
        return 200, {"ok": True}

    proxy = ProviderProxy(r, fwd)
    status, _ = proxy.complete(
        "/chat/completions",
        {
            "model": "*",
            "messages": [{"role": "user", "content": "hi"}],
            "_mac_context": {"agent_id": "agent_a", "request_id": "req_a"},
            "mac_context": {"agent_id": "agent_b"},
        },
    )
    assert status == 200
    assert "_mac_context" not in seen
    assert "mac_context" not in seen
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
    assert {"/v1/chat/completions", "/v1/responses", "/v1/embeddings"} <= paths


def test_mount_router_endpoint_surface_is_reachable_and_content_correct(monkeypatch):
    import json as _json

    from fastapi import FastAPI
    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient

    from mac import router_app

    calls = []

    def fwd(provider, path, payload, *, timeout=60.0):
        calls.append((provider.name, path, payload))
        if path == "/embeddings":
            return 200, {"object": "list", "data": [{"embedding": [0.1, 0.2], "index": 0}]}
        return 200, {
            "id": "chatcmpl_router_surface",
            "choices": [{"message": {"role": "assistant", "content": "ready"}}],
            "model": payload["model"],
        }

    class _Headers:
        @staticmethod
        def get(key, default=None):
            return "application/json" if key.lower() == "content-type" else default

    class _Resp:
        status = 200
        headers = _Headers()

        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return _json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=60.0):
        url = req.full_url
        if url.endswith("/video/generate"):
            return _Resp({"job_id": "upstream_job_1", "status": "running"})
        if url.endswith("/video/jobs/upstream_job_1"):
            return _Resp({"status": "completed", "artifacts": [{"base64": "VIDEO"}]})
        if "/v1/genai/" in url:
            return _Resp({"artifacts": [{"base64": "IMAGE"}]})
        if "/v1/audio/" in url:
            return _Resp({"transcript": "audio ok"})
        if "/v1/video/" in url:
            return _Resp({"status": "accepted"})
        raise AssertionError("unexpected router upstream URL %s" % url)

    monkeypatch.setattr(router_app.urllib.request, "urlopen", fake_urlopen)

    app = FastAPI()
    env = {
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_IMAGE_UPSTREAM": "https://media.example/v1/genai",
        "MAC_ROUTER_IMAGE_KEY": "secret:image",
        "MAC_ROUTER_AUDIO_UPSTREAM": "https://media.example/v1/audio",
        "MAC_ROUTER_AUDIO_KEY": "secret:audio",
        "MAC_ROUTER_VIDEO_UPSTREAM": "https://media.example/v1/video",
        "MAC_ROUTER_VIDEO_KEY": "secret:video",
        "MAC_ROUTER_MEDIA_JSON": _json.dumps(
            {
                "video.generate": [
                    {
                        "provider": "nvidia-video",
                        "base_url": "https://media.example/v1",
                        "model": "animatediff",
                        "key": "secret:video",
                        "adapter": "video_generate",
                    }
                ]
            }
        ),
    }
    proxy = ProviderProxy(_router(), fwd, default_model="meta/llama-route-test")
    resolver = {"image": "IMAGE_KEY", "audio": "AUDIO_KEY", "video": "VIDEO_KEY"}.get
    assert mount_router(app, env=env, proxy=proxy, secret_resolver=resolver) is True

    route_keys = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }
    assert route_keys == {
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/responses"),
        ("POST", "/v1/embeddings"),
        ("POST", "/v1/genai/{path:path}"),
        ("POST", "/v1/audio/{path:path}"),
        ("POST", "/v1/video/{path:path}"),
        ("POST", "/v1/media/{op}"),
        ("GET", "/v1/media/jobs/{job_id}"),
    }

    client = TestClient(app)
    chat = client.post("/v1/chat/completions", json={"model": "*", "messages": [{"role": "user", "content": "hi"}]})
    assert chat.status_code == 200
    assert chat.json()["choices"][0]["message"]["content"] == "ready"
    assert calls[-1][1] == "/chat/completions"
    assert calls[-1][2]["model"] == "meta/llama-route-test"

    responses = client.post(
        "/v1/responses",
        json={
            "model": "*",
            "instructions": "Be concise",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        },
    )
    assert responses.status_code == 200
    assert responses.json()["object"] == "response"
    assert responses.json()["output"][0]["content"][0]["text"] == "ready"
    assert calls[-1][1] == "/chat/completions"
    assert calls[-1][2]["messages"] == [
        {"role": "system", "content": "Be concise"},
        {"role": "user", "content": "hi"},
    ]
    assert calls[-1][2]["model"] == "meta/llama-route-test"

    embeddings = client.post("/v1/embeddings", json={"model": "*", "input": "hello"})
    assert embeddings.status_code == 200
    assert embeddings.json()["data"][0]["embedding"] == [0.1, 0.2]
    assert calls[-1][1] == "/embeddings"

    image = client.post("/v1/genai/black-forest-labs/flux.1-dev", json={"prompt": "image"})
    assert image.status_code == 200
    assert image.json()["artifacts"][0]["base64"] == "IMAGE"

    audio = client.post("/v1/audio/transcriptions", json={"audio": "QUJD"})
    assert audio.status_code == 200
    assert audio.json()["transcript"] == "audio ok"

    video = client.post("/v1/video/nvidia/cosmos-predict", json={"prompt": "clip"})
    assert video.status_code == 200
    assert video.json()["status"] == "accepted"

    submit = client.post("/v1/media/video.generate", json={"prompt": "movie"})
    assert submit.status_code == 200
    assert submit.json()["job_id"].startswith("mjob_")
    assert submit.json()["provider"] == "nvidia-video"

    poll = client.get("/v1/media/jobs/%s" % submit.json()["job_id"])
    assert poll.status_code == 200
    assert poll.json()["status"] == "completed"
    assert poll.json()["artifacts"][0]["base64"] == "VIDEO"


def test_mount_records_route_context_from_headers_and_strips_internal_body_context():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    observed = []
    seen = {}

    def fwd(provider, path, payload, *, timeout=60.0):
        seen.update(payload)
        return 200, {"ok": True}

    app = FastAPI()
    proxy = ProviderProxy(_router(), fwd, route_observer=observed.append)
    assert mount_router(app, env={"MAC_ROUTER_BACKEND": "inproc"}, proxy=proxy) is True

    resp = TestClient(app).post(
        "/v1/chat/completions",
        headers={
            "X-MAC-Agent-ID": "agent_header",
            "X-MAC-Task-ID": "task_header",
            "X-MAC-Command-ID": "cmd_header",
        },
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "do work"}],
            "_mac_context": {"agent_id": "agent_body", "task_id": "task_body"},
        },
    )

    assert resp.status_code == 200
    assert "_mac_context" not in seen
    assert observed[0]["agent_id"] == "agent_header"
    assert observed[0]["task_id"] == "task_header"
    assert observed[0]["command_id"] == "cmd_header"
    assert "do work" not in str(observed[0])


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
    p3 = Provider("local", "http://127.0.0.1:8000/v1", api_key_env="none")
    assert resolve_provider_key(p3) == ""                   # explicit private no-auth


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


def test_audio_and_video_proxies_forward_to_their_upstreams(monkeypatch):
    # Speech (ASR/TTS) and video-gen are independent modality proxies, each gated
    # on its own upstream+key and routing through the hub with a vault key.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mac import router_app

    captured = []

    class _Resp:
        status = 200

        def read(self):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        router_app.urllib.request, "urlopen",
        lambda req, timeout=60.0: captured.append((req.full_url, req.headers.get("Authorization"))) or _Resp(),
    )

    app = FastAPI()
    env = {
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_AUDIO_UPSTREAM": "https://ai.api.nvidia.com/v1/audio",
        "MAC_ROUTER_AUDIO_KEY": "secret:nvidia-audio",
        "MAC_ROUTER_VIDEO_UPSTREAM": "https://ai.api.nvidia.com/v1/video",
        "MAC_ROUTER_VIDEO_KEY": "secret:nvidia-video",
    }
    resolver = {"nvidia-audio": "audio-key", "nvidia-video": "video-key"}.get
    assert mount_router(app, env=env, secret_resolver=resolver) is True

    client = TestClient(app)
    assert client.post("/v1/audio/transcriptions", json={"x": 1}).status_code == 200
    assert client.post("/v1/video/nvidia/cosmos-predict", json={"prompt": "x"}).status_code == 200

    urls = dict(captured)
    assert urls["https://ai.api.nvidia.com/v1/audio/transcriptions"] == "Bearer audio-key"
    assert urls["https://ai.api.nvidia.com/v1/video/nvidia/cosmos-predict"] == "Bearer video-key"


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


def test_build_proxy_never_uses_gpt_41_mini_for_wildcard_default():
    env = {
        "MAC_ROUTER_PROVIDERS": "p=http://p/v1,0",
        "MAC_ROUTER_BACKEND": "inproc",
        "MAC_ROUTER_WILDCARD_MODELS": "us/azure/openai/gpt-4.1-mini|azure/openai/gpt-4.1-mini",
        "MAC_ROUTER_DEFAULT_MODEL": "us/azure/openai/gpt-4.1-mini",
        "MAC_HERMES_GATEWAY_MODEL": "azure/anthropic/claude-sonnet-4-6",
    }
    proxy = build_proxy_from_env(env)
    assert proxy is not None
    assert proxy._wildcard_models == ("azure/anthropic/claude-sonnet-4-6",)
    assert proxy._default_model == "azure/anthropic/claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# mac_route_context_headers
# ---------------------------------------------------------------------------

def test_mac_route_context_headers_explicit_values():
    """Explicit arguments are echoed as lowercase header keys."""
    headers = mac_route_context_headers(
        agent_id="agent_abc",
        task_id="task_xyz",
        lease_id="lease_123",
        persona_instance_id="persona_hi",
        command_id="cmd_999",
        fleet="fleet_main",
        request_id="req_001",
    )
    assert headers["x-mac-agent-id"] == "agent_abc"
    assert headers["x-mac-task-id"] == "task_xyz"
    assert headers["x-mac-lease-id"] == "lease_123"
    assert headers["x-mac-persona-instance-id"] == "persona_hi"
    assert headers["x-mac-command-id"] == "cmd_999"
    assert headers["x-mac-fleet"] == "fleet_main"
    assert headers["x-mac-request-id"] == "req_001"


def test_mac_route_context_headers_from_env():
    """Falls back to env-vars when no explicit arguments are given."""
    env = {
        "MAC_AGENT_ID": "agent_from_env",
        "MAC_TASK_ID": "task_from_env",
        "MAC_LEASE_ID": "lease_from_env",
        "MAC_PERSONA_INSTANCE_ID": "persona_from_env",
        "MAC_COMMAND_ID": "cmd_from_env",
        "MAC_FLEET": "fleet_from_env",
    }
    headers = mac_route_context_headers(env=env)
    assert headers["x-mac-agent-id"] == "agent_from_env"
    assert headers["x-mac-task-id"] == "task_from_env"
    assert headers["x-mac-lease-id"] == "lease_from_env"
    assert headers["x-mac-persona-instance-id"] == "persona_from_env"
    assert headers["x-mac-command-id"] == "cmd_from_env"
    assert headers["x-mac-fleet"] == "fleet_from_env"


def test_mac_route_context_headers_explicit_overrides_env():
    """Explicit argument takes precedence over env-var."""
    env = {"MAC_AGENT_ID": "agent_env", "MAC_TASK_ID": "task_env"}
    headers = mac_route_context_headers(agent_id="agent_explicit", env=env)
    assert headers["x-mac-agent-id"] == "agent_explicit"
    assert headers["x-mac-task-id"] == "task_env"


def test_mac_route_context_headers_empty_env_omits_keys():
    """Empty env and no explicit args returns an empty dict (no blank headers)."""
    headers = mac_route_context_headers(env={})
    assert headers == {}


def test_mac_route_context_headers_truncates_long_values():
    """Values longer than 256 characters are truncated."""
    long_val = "a" * 300
    headers = mac_route_context_headers(agent_id=long_val)
    assert len(headers["x-mac-agent-id"]) == 256


def test_mac_route_context_headers_only_nonempty_keys_included():
    """Only non-empty values produce header entries."""
    headers = mac_route_context_headers(agent_id="agent_x", env={})
    assert set(headers.keys()) == {"x-mac-agent-id"}


def test_mac_route_context_headers_is_exported():
    """mac_route_context_headers must appear in __all__."""
    from mac import router_app
    assert "mac_route_context_headers" in router_app.__all__


# ---------------------------------------------------------------------------
# Principal-mismatch hardening — _is_principal_mismatch_rejected() + 403 gate
# ---------------------------------------------------------------------------

def test_reject_mismatch_env_var_truthy_values():
    """All truthy env var spellings activate the mismatch gate."""
    for val in ("1", "true", "True", "TRUE", "yes", "on"):
        assert _ra._is_principal_mismatch_rejected({_ra._REJECT_MISMATCH_ENV: val}) is True


def test_reject_mismatch_env_var_falsy_values():
    """Falsy env var spellings keep the default (pass-through) behavior."""
    for val in ("0", "false", "False", "no", "off", ""):
        assert _ra._is_principal_mismatch_rejected({_ra._REJECT_MISMATCH_ENV: val}) is False


def test_reject_mismatch_disabled_by_default():
    """Without the env var the gate is off (safe default for existing hubs)."""
    assert _ra._is_principal_mismatch_rejected({}) is False


def test_mismatch_is_logged_but_not_rejected_by_default():
    """With the default env the principal overrides the claimed header without
    raising — the mismatch is captured in claimed_agent_id for audit."""
    from types import SimpleNamespace
    from mac.router_app import _route_context_from_request

    class FakeRequest:
        headers = {"x-mac-agent-id": "agent_spoofed"}
        state = SimpleNamespace(principal=SimpleNamespace(agent_id="agent_real"))

    ctx = _route_context_from_request(FakeRequest(), {})
    assert ctx["agent_id"] == "agent_real"
    assert ctx.get("claimed_agent_id") == "agent_spoofed"


def test_mismatch_is_rejected_when_env_var_enabled():
    """_is_principal_mismatch_rejected returns True when the env var is 1."""
    env = {_ra._REJECT_MISMATCH_ENV: "1"}
    assert _ra._is_principal_mismatch_rejected(env) is True


def test_mount_router_returns_403_on_mismatch_when_rejection_enabled():
    """End-to-end: /v1/chat/completions returns 403 when rejection is active
    and the fake request carries a mismatched claimed_agent_id."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Patch _route_context_from_request to inject a mismatch without needing
    # a real JWT middleware.
    original = _ra._route_context_from_request

    def _faked_ctx(request, body):
        ctx = original(request, body)
        ctx["agent_id"] = "agent_real"
        ctx["claimed_agent_id"] = "agent_spoofed"
        return ctx

    fwd, _ = _fake_forward({"primary": (200, {"ok": True})})
    proxy = ProviderProxy(_router(), fwd)
    app = FastAPI()
    env = {"MAC_ROUTER_BACKEND": "inproc", _ra._REJECT_MISMATCH_ENV: "1"}
    assert mount_router(app, env=env, proxy=proxy) is True

    _ra._route_context_from_request = _faked_ctx
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/v1/chat/completions", json={"model": "m"})
        assert resp.status_code == 403
        err = resp.json()
        assert err["error"]["type"] == "principal_mismatch"
        assert "agent_real" in err["error"]["message"]
        assert "agent_spoofed" in err["error"]["message"]
    finally:
        _ra._route_context_from_request = original


def test_mount_router_embeddings_returns_403_on_mismatch_when_rejection_enabled():
    """/v1/embeddings also enforces the 403 gate when rejection is active."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    original = _ra._route_context_from_request

    def _faked_ctx(request, body):
        ctx = original(request, body)
        ctx["agent_id"] = "agent_token"
        ctx["claimed_agent_id"] = "agent_header"
        return ctx

    fwd, _ = _fake_forward({"primary": (200, {"ok": True})})
    proxy = ProviderProxy(_router(), fwd)
    app = FastAPI()
    env = {"MAC_ROUTER_BACKEND": "inproc", _ra._REJECT_MISMATCH_ENV: "1"}
    assert mount_router(app, env=env, proxy=proxy) is True

    _ra._route_context_from_request = _faked_ctx
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/v1/embeddings", json={"model": "m", "input": "hi"})
        assert resp.status_code == 403
        assert resp.json()["error"]["type"] == "principal_mismatch"
    finally:
        _ra._route_context_from_request = original


def test_no_mismatch_passes_through_when_rejection_enabled():
    """When rejection is active but there is no mismatch (no claimed_agent_id),
    the request proceeds normally."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fwd, _ = _fake_forward({"primary": (200, {"ok": True})})
    proxy = ProviderProxy(_router(), fwd)
    app = FastAPI()
    env = {"MAC_ROUTER_BACKEND": "inproc", _ra._REJECT_MISMATCH_ENV: "1"}
    assert mount_router(app, env=env, proxy=proxy) is True
    client = TestClient(app)
    # No X-MAC-Agent-ID header, no principal → no mismatch
    resp = client.post("/v1/chat/completions", json={"model": "*"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Live llm.route attribution join-field verification
# ---------------------------------------------------------------------------


def test_llm_route_rows_carry_attribution_in_observation():
    """Verify that a route observation emitted with task+agent context carries
    all fields needed for hub-side llm.route row joins.

    This is a unit-level simulation: the route_observer receives exactly what
    the hub persists in the llm.route table.  Missing agent_id or task_id means
    the row cannot be joined to task work — this test asserts they survive the
    observation pipeline unchanged when supplied via route_context.
    """
    observed = []
    r = _router()
    fwd, _ = _fake_forward({"primary": (200, {
        "model": "test-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    })})
    proxy = ProviderProxy(r, fwd, route_observer=observed.append)

    proxy.complete(
        "/chat/completions",
        {"model": "test-model"},
        route_context={
            "agent_id": "agent_live_test",
            "task_id": "task_live_test",
            "lease_id": "lease_live_test",
            "persona_instance_id": "persona_live_test",
        },
    )

    assert len(observed) == 1
    ev = observed[0]
    # All attribution fields must survive unchanged.
    assert ev["agent_id"] == "agent_live_test", "agent_id missing from llm.route observation"
    assert ev["task_id"] == "task_live_test",   "task_id missing from llm.route observation"
    assert ev["lease_id"] == "lease_live_test", "lease_id missing from llm.route observation"
    assert ev["persona_instance_id"] == "persona_live_test", "persona_instance_id missing"
    # Usage must propagate for cost attribution.
    assert ev["usage"]["total_tokens"] == 30
    # The row is joinable: schema + provider + model are present.
    assert ev["schema"] == "mac.llm_route.v1"
    assert ev["provider"] == "primary"
    assert ev["resolved_model"] == "test-model"
    assert ev["status_code"] == 200


def test_llm_route_mac_route_context_headers_feeds_attribution():
    """Demonstrate the full attribution chain: mac_route_context_headers()
    produces headers → they are read by _route_context_from_request → the
    observer receives the attribution context.

    This proves the client-side helper and the server-side extraction are
    aligned so a stamped worker request produces a fully-attributable row."""
    from types import SimpleNamespace
    from mac.router_app import _route_context_from_request

    # mac_route_context_headers() returns {'x-mac-agent-id': ..., ...}
    stamped = mac_route_context_headers(
        agent_id="agent_chain_test",
        task_id="task_chain_test",
        persona_instance_id="persona_chain_test",
    )

    class FakeRequest:
        headers = stamped  # inject stamped headers directly
        state = SimpleNamespace(principal=None)

    ctx = _route_context_from_request(FakeRequest(), {})
    assert ctx["agent_id"] == "agent_chain_test"
    assert ctx["task_id"] == "task_chain_test"
    assert ctx["persona_instance_id"] == "persona_chain_test"
    # No mismatch — no principal, so claimed_agent_id must not appear.
    assert "claimed_agent_id" not in ctx
