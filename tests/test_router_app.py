"""th-merge-02: the in-mac OpenAI front door (ProviderProxy + gated mount)."""

from __future__ import annotations

from mac.provider_router import Provider, ProviderRouter
from mac.router_app import ProviderProxy, build_proxy_from_env, mount_router


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
